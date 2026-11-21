"""Measure what each test really costs to run inside a forked runner child.

usage: python measure_cpu.py <sqlglot checkout> <tests.json> <out.json> [repeats]

Recorded seconds include pytest overhead and process-pool spawns; in a warm
forked child most tests are far cheaper. Writes
  {"tests": {id: cpu_ms}, "setup": {"file::Class": cpu_ms}, "import": {file: cpu_ms}}
measured on this machine (the selector scales them by BR_CPU_FACTOR).
"""

import json
import os
import resource
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import runner  # noqa: E402

root = os.path.abspath(sys.argv[1])
tests = json.loads(Path(sys.argv[2]).read_text())["tests"]
out_path = Path(sys.argv[3])
repeats = int(sys.argv[4]) if len(sys.argv) > 4 else 2


def cpu():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def measure_file(path, ids, wfd):
    import concurrent.futures
    import importlib

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    concurrent.futures.ProcessPoolExecutor = runner._InlineExecutor
    res = {"tests": {}, "setup": {}, "import": {}, "warmup": {}, "fail": []}
    t0 = cpu()
    mod = importlib.import_module(path[:-3].replace("/", "."))
    res["import"][path] = (cpu() - t0) * 1000
    by_class = {}
    for tid in ids:
        by_class.setdefault(tid.split("::")[1], []).append(tid)
    # cold pass: what a fresh child pays for this file (lazy dialect loads, caches)
    t_cold = cpu()
    for cls_name, cids in by_class.items():
        cls = getattr(mod, cls_name)
        suite = unittest.TestSuite([cls(t.split("::")[2]) for t in cids])
        suite.run(unittest.TestResult())
    cold = (cpu() - t_cold) * 1000
    # warm pass: per test
    for cls_name, cids in by_class.items():
        cls = getattr(mod, cls_name)
        t0 = cpu()
        try:
            cls.setUpClass()
        except Exception:  # noqa: BLE001
            pass
        res["setup"][f"{path}::{cls_name}"] = (cpu() - t0) * 1000
        for tid in cids:
            case = cls(tid.split("::")[2])
            result = unittest.TestResult()
            t0 = cpu()
            case.run(result)  # run() without a suite: no class fixtures re-run
            res["tests"][tid] = (cpu() - t0) * 1000
            if result.failures or result.errors or result.unexpectedSuccesses:
                res["fail"].append(tid)
        try:
            cls.tearDownClass()
        except Exception:  # noqa: BLE001
            pass
    warm = sum(res["tests"].values()) + sum(res["setup"].values())
    res["warmup"][path] = max(cold - warm, 0.0)
    os.write(wfd, json.dumps(res).encode())
    os._exit(0)


runner.preload()
files = {}
for t in tests:
    files.setdefault(t["file"], []).append(t["id"])

best = {"tests": {}, "setup": {}, "import": {}, "warmup": {}}
baseline_fail = set()
for rep in range(repeats):
    for path, ids in sorted(files.items()):
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            try:
                measure_file(path, ids, wfd)
            finally:
                os._exit(1)
        os.close(wfd)
        buf = runner._read_until(rfd, pid, 600)
        try:
            res = json.loads(buf.decode())
        except ValueError:
            print(f"failed: {path}", file=sys.stderr)
            continue
        baseline_fail.update(res["fail"])
        for k in best:
            for name, v in res[k].items():
                best[k][name] = min(best[k].get(name, v), v)  # least noisy
out = {k: {n: round(v, 1) for n, v in d.items()} for k, d in best.items()}
out["baseline_fail"] = sorted(baseline_fail)
out["bench_ms"] = round(min(runner.bench_ms() for _ in range(3)), 2)  # fail in a runner child with no patch: never trusted
out_path.write_text(json.dumps(out))
print(f"baseline failures in the runner: {sorted(baseline_fail)}", file=sys.stderr)
tot = sum(best["tests"].values()) / 1000
print(f"measured {len(best['tests'])} tests: {tot:.1f}s run, "
      f"{sum(best['setup'].values()) / 1000:.1f}s class setup, {sum(best['import'].values()) / 1000:.1f}s imports",
      file=sys.stderr)
