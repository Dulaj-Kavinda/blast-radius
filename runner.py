"""Run a few cheap sqlglot tests against a patched tree, inside the scoring run.

The selector calls verify() for commits where running the cheap tests it
picked is likely to cost less than listing them. The parent preloads the heavy
third-party modules once. Each commit runs in a forked child that serves the
patched Python files from memory (nothing is written: the disk is read-only),
imports sqlglot and the test modules fresh, and runs the tests with unittest,
stopping at the first failure.

The result for each test is "pass", "fail" or missing (not run). Only tests
that ran and passed are ever dropped by the caller.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import select
import signal
import sys
import time


def bench_ms(rounds: int = 15) -> float:
    """CPU ms of a fixed pure-Python workload (dicts, strings, calls: like
    sqlglot's), best of `rounds`. Compared with the same number measured when
    the map was built, it calibrates per-test CPU costs to this machine."""
    best = float("inf")
    for _ in range(rounds):
        t0 = time.process_time()
        d: dict = {}
        acc = 0
        for i in range(40000):
            k = "k" + str(i % 997)
            d[k] = d.get(k, 0) + i
            acc += len(k.upper()) if i % 3 else hash(k) & 7
        best = min(best, (time.process_time() - t0) * 1000)
    return best


def _debug(what: str) -> None:
    """BR_RUNNER_DEBUG=<file>: append child tracebacks there (local debugging)."""
    path = os.environ.get("BR_RUNNER_DEBUG")
    if path:
        import traceback

        with open(path, "a") as f:
            f.write(f"--- {what}\n{traceback.format_exc()}\n")


def preload() -> bool:
    """Import the heavy, never-patched dependencies before forking."""
    try:
        import concurrent.futures  # noqa: F401
        import pickle  # noqa: F401
        import unittest  # noqa: F401
        import unittest.mock  # noqa: F401

        import duckdb  # noqa: F401
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import pandas.testing  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"runner: preload failed ({e}); verification disabled", file=sys.stderr)
        return False
    return True


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, source: str):
        super().__init__(fullname, path)
        self._source = source

    def get_data(self, path):
        if path == self.path:
            return self._source.encode("utf-8")
        return super().get_data(path)

    def get_code(self, fullname):
        return compile(self._source, self.path, "exec", dont_inherit=True)


class _Overlay(importlib.abc.MetaPathFinder):
    def __init__(self, root: str, files: dict[str, str]):
        self.mods = {}
        for rel, src in files.items():
            if not rel.endswith(".py"):
                continue
            name = rel[:-3].replace("/", ".")
            is_pkg = name.endswith(".__init__")
            if is_pkg:
                name = name[: -len(".__init__")]
            self.mods[name] = (os.path.join(root, rel), src, is_pkg)

    def find_spec(self, fullname, path=None, target=None):
        hit = self.mods.get(fullname)
        if hit is None:
            return None
        p, src, is_pkg = hit
        loader = _PatchedLoader(fullname, p, src)
        spec = importlib.util.spec_from_file_location(
            fullname, p, loader=loader, submodule_search_locations=[os.path.dirname(p)] if is_pkg else None
        )
        return spec


def _roundtrip(obj):
    import pickle

    return pickle.loads(pickle.dumps(obj))


class _InlineExecutor:
    """ProcessPoolExecutor stand-in: a spawned worker would import the
    unpatched files from disk. Pickling is kept (Expression.__reduce__)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            fut.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            _debug("child")
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, **kwargs):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_open_overlay(root: str, files: dict[str, str]) -> None:
    """Serve the patched text of changed files to open() / Path.read_text, for
    fixtures and for sources the tests read as text. Writes go to the disk."""
    import builtins
    import io

    data = {os.path.realpath(os.path.join(root, rel)): src for rel, src in files.items()}
    if not data:
        return
    real_open = io.open

    def open_(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and not any(c in mode for c in "wax+"):
            try:
                src = data.get(os.path.realpath(os.fsdecode(file)))
            except (TypeError, ValueError):
                src = None
            if src is not None:
                return io.BytesIO(src.encode("utf-8")) if "b" in mode else io.StringIO(src)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = open_
    io.open = open_


def _enter(root: str, files: dict[str, str], pools: bool = True) -> str:
    """Common child set-up: silence output, serve the patched tree."""
    import concurrent.futures

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    root = os.path.abspath(root)
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    if pools:
        concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    sys.meta_path.insert(0, _Overlay(root, files))
    _install_open_overlay(root, files)
    return root


_FX = {"allowed": None, "k": 0}


def _fx_loader(orig):
    """A fixture loader that yields only the entries chosen for the running
    test. Entries are numbered across every loader call of the test, as in the
    map (tools/mapplugin.py); outside a test it is the original loader."""

    def loader(filename):
        if _FX["allowed"] is None:
            yield from orig(filename)
            return
        for item in orig(filename):
            k = _FX["k"]
            _FX["k"] += 1
            if k in _FX["allowed"]:
                yield item

    loader._fxwrapped = True
    return loader


def _install_fx(mod) -> None:
    helpers = sys.modules.get("tests.helpers")
    if helpers is None:
        return
    if not getattr(helpers.load_sql_fixture_pairs, "_fxwrapped", False):
        helpers._fx_orig = (helpers.load_sql_fixture_pairs, helpers.load_sql_fixtures)
        helpers.load_sql_fixture_pairs = _fx_loader(helpers._fx_orig[0])
        helpers.load_sql_fixtures = _fx_loader(helpers._fx_orig[1])
    pairs, single = helpers._fx_orig
    if getattr(mod, "load_sql_fixture_pairs", None) is pairs:
        mod.load_sql_fixture_pairs = helpers.load_sql_fixture_pairs
    if getattr(mod, "load_sql_fixtures", None) is single:
        mod.load_sql_fixtures = helpers.load_sql_fixtures


def _child(
    root: str, files: dict[str, str], test_ids: list[str], wfd: int, max_failures: int = 1, subsets=None
) -> None:
    import concurrent.futures
    import unittest

    root = _enter(root, files)
    subsets = {t: set(v) for t, v in (subsets or {}).items()}

    results: dict[str, str] = {}

    def put(tid, verdict):
        if results.get(tid) == "fail":
            return
        results[tid] = verdict
        os.write(wfd, (json.dumps([tid, verdict]) + "\n").encode())

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            _FX["allowed"] = subsets.get(getattr(test, "_br_id", None))
            _FX["k"] = 0

        def stopTest(self, test):
            _FX["allowed"] = None
            super().stopTest(test)

        def _mark(self, test, ok):
            tid = getattr(test, "_br_id", None)
            if tid is None:
                # setUpClass / module errors: every test of that class fails
                desc = str(test)
                for t in test_ids:
                    cls = t.split("::")[1] if t.count("::") >= 2 else ""
                    if cls and cls in desc:
                        put(t, "fail")
                self.stop()
                return
            if not ok:
                put(tid, "fail")
                if sum(1 for v in results.values() if v == "fail") >= max_failures:
                    self.stop()
            else:
                put(tid, "pass")

        def addSuccess(self, test):
            self._mark(test, True)

        def addFailure(self, test, err):
            self._mark(test, False)

        def addError(self, test, err):
            self._mark(test, False)

        def addSkip(self, test, reason):
            self._mark(test, True)

        def addExpectedFailure(self, test, err):
            self._mark(test, True)

        def addUnexpectedSuccess(self, test):
            self._mark(test, False)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test, False)

    try:
        suite = unittest.TestSuite()
        for tid in test_ids:
            path, cls_name, meth = tid.split("::")
            mod = importlib.import_module(path[:-3].replace("/", "."))
            if subsets:
                _install_fx(mod)
            case = getattr(mod, cls_name)(meth)
            case._br_id = tid
            suite.addTest(case)
        result = Result()
        # a test that never reports (killed by the stop) stays "not run"
        suite.run(result)
    except BaseException:  # noqa: BLE001
        _debug("child")
        # an import error fails every test of that module
        for tid in test_ids:
            if tid not in results:
                put(tid, "fail")
    os._exit(0)


def _import_child(root: str, files: dict[str, str], wfd: int) -> None:
    import concurrent.futures
    import glob
    import pkgutil

    root = _enter(root, files)
    failed = []
    try:
        import sqlglot

        names = [i.name for i in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot.")]
    except BaseException:  # noqa: BLE001
        _debug("child")
        failed.append("sqlglot")
        names = []
    names += sorted(f[:-3].replace("/", ".") for f in glob.glob("tests/**/*.py", recursive=True))
    for name in names:
        if name.endswith("__main__") or name in sys.modules and sys.modules[name] is not None:
            continue
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            _debug("child")
            failed.append(name)
    os.write(wfd, (json.dumps(failed) + "\n").encode())
    os._exit(0)


def _tpc_child(root, files, test_name, indices, wfd):
    import concurrent.futures

    root = _enter(root, files)
    verdict = "fail"
    try:
        mod = importlib.import_module("tests.test_executor")
        cls = mod.TestExecutor
        cls.setUpClass()
        case = cls(test_name)
        tpch = test_name == "test_execute_tpch"
        schema = mod.TPCH_SCHEMA if tpch else mod.TPCDS_SCHEMA
        tables = cls.tpch_tables if tpch else cls.tpcds_tables
        sqls = cls.tpch_sqls if tpch else cls.tpcds_sqls
        # the real test pickles these to a spawned worker; executor tables hold no sqlglot objects
        mod.initializer(schema, tables)
        outs = []
        for i in indices:
            args, sql, _ = sqls[i]
            expr = _roundtrip(mod.parse_one(sql))
            outs.append((i, _roundtrip(mod.mp_execute(expr, _roundtrip(args)))))
        for i, table in outs:
            if table is not None:
                case.subtestHelper(i, table, tpch=tpch)
        verdict = "pass"
    except BaseException:  # noqa: BLE001
        _debug("child")
        verdict = "fail"
    os.write(wfd, (json.dumps(verdict) + "\n").encode())
    os._exit(0)


def run_tpc_queries(root: str, files: dict[str, str], test_name: str, indices: list[int], timeout: float):
    """Replay only some TPC queries of TestExecutor.test_execute_tpch / _tpcds.
    Returns "pass", "fail" or None (timed out / crashed)."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _tpc_child(root, files, test_name, indices, wfd)
        finally:
            os._exit(1)
    os.close(wfd)
    buf = _read_until(rfd, pid, timeout)
    try:
        return json.loads(buf.decode().strip().split("\n")[-1])
    except (ValueError, IndexError):
        return None


def import_check(root: str, files: dict[str, str], timeout: float = 20.0):
    """Import every sqlglot and test module with files overlaid.
    Returns the modules that failed to import, or None if the check died."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _import_child(root, files, wfd)
        finally:
            os._exit(1)
    os.close(wfd)
    buf = _read_until(rfd, pid, timeout)
    try:
        return json.loads(buf.decode().strip().split("\n")[-1])
    except (ValueError, IndexError):
        return None


def _read_until(rfd: int, pid: int, timeout: float) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            break
        r, _, _ = select.select([rfd], [], [], left)
        if not r:
            continue
        chunk = os.read(rfd, 1 << 16)
        if not chunk:
            break
        buf += chunk
    os.close(rfd)
    os.waitpid(pid, 0)
    return buf


def verify(
    root: str, files: dict[str, str], test_ids: list[str], timeout: float, max_failures: int = 1, subsets=None
) -> dict[str, str]:
    """Run test_ids against root with files overlaid. Returns {id: pass|fail}."""
    if not test_ids:
        return {}
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _child(root, files, test_ids, wfd, max_failures, subsets)
        finally:
            os._exit(1)
    os.close(wfd)
    buf = _read_until(rfd, pid, timeout)
    out: dict[str, str] = {}
    for line in buf.decode(errors="replace").split("\n"):
        try:
            tid, verdict = json.loads(line)
        except ValueError:
            continue
        if out.get(tid) != "fail":
            out[tid] = verdict
    return out


# --------------------------------------------------------------- TPC fingerprints


def _canon(v, steps):
    """A deterministic, JSON-able view of planner objects and expressions."""
    from sqlglot import exp
    from sqlglot.planner import Step
    from sqlglot.serde import dump

    if isinstance(v, exp.Expr):
        return ["E", dump(v)]
    if isinstance(v, Step):
        if id(v) not in steps:
            steps[id(v)] = None  # placeholder against cycles
            fields = [[k, _canon(x, steps)] for k, x in sorted(vars(v).items()) if k not in ("dependencies", "dependents")]
            for d in v.dependencies:
                _canon(d, steps)
            steps[id(v)] = [type(v).__name__, fields, sorted(d.name for d in v.dependencies)]
        return ["S", v.name]
    if isinstance(v, dict):
        return ["D", sorted([[_canon(k, steps), _canon(x, steps)] for k, x in v.items()], key=repr)]
    if isinstance(v, (set, frozenset)):
        return ["T", sorted((_canon(x, steps) for x in v), key=repr)]
    if isinstance(v, (list, tuple)):
        return ["L", [_canon(x, steps) for x in v]]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return ["O", type(v).__name__, repr(v)]


def tpc_fingerprints(test_name: str, indices=None) -> list[str]:
    """Per-query fingerprint of everything the TPC executor test derives from
    sqlglot before running on data: the parsed (and pickled) expression, the
    tables it reads, the optimized expression, the plan, and the duckdb SQL
    the expected result comes from. Must run inside a process where the
    module overlay (if any) is installed."""
    import hashlib
    import pickle

    from sqlglot import parse_one, transpile
    from sqlglot.executor import Plan, optimize
    from sqlglot.schema import ensure_schema

    mod = importlib.import_module("tests.test_executor")
    helpers = importlib.import_module("tests.helpers")
    tpch = test_name == "test_execute_tpch"
    path = "optimizer/tpc-h/tpc-h.sql" if tpch else "optimizer/tpc-ds/tpc-ds.sql"
    sqls = list(helpers.load_sql_fixture_pairs(path))
    # TestExecutor.setUpClass loads both fixture files: an error there fails every query
    for other in ("optimizer/tpc-h/tpc-h.sql", "optimizer/tpc-ds/tpc-ds.sql"):
        list(helpers.load_sql_fixture_pairs(other))
    schema_dict = mod.TPCH_SCHEMA if tpch else mod.TPCDS_SCHEMA
    out = []
    for i in range(len(sqls)) if indices is None else indices:
        args, sql, _ = sqls[i]
        try:
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            parts = [_canon(expr, {})]
            if args.get("execute"):
                parts.append(sorted(t.name for t in mod.find_tables(expr)))
                optimized = optimize(expr, ensure_schema(schema_dict), leave_tables_isolated=True)
                parts.append(_canon(optimized, {}))
                steps: dict = {}
                plan = Plan(optimized)
                parts.append(_canon(plan.root, steps))
                parts.append(sorted((s for s in steps.values() if s is not None), key=repr))
                parts.append(transpile(sql, write="duckdb")[0])
            blob = json.dumps(parts, sort_keys=True, default=repr)
        except BaseException as e:  # noqa: BLE001
            _debug("child")
            blob = f"error {type(e).__name__}: {e}"
        out.append(hashlib.sha256(blob.encode()).hexdigest()[:16])
    return out


def _fp_child(root, files, test_name, indices, wfd):
    import concurrent.futures

    root = _enter(root, files)
    try:
        out = tpc_fingerprints(test_name, indices)
    except BaseException:  # noqa: BLE001
        _debug("child")
        out = None
    os.write(wfd, (json.dumps(out) + "\n").encode())
    os._exit(0)


def tpc_fingerprint_check(root: str, files: dict[str, str], test_name: str, indices: list[int], timeout: float):
    """Fingerprints of the given queries with files overlaid, or None."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _fp_child(root, files, test_name, indices, wfd)
        finally:
            os._exit(1)
    os.close(wfd)
    buf = _read_until(rfd, pid, timeout)
    try:
        return json.loads(buf.decode().strip().split("\n")[-1])
    except (ValueError, IndexError):
        return None


# ----------------------------------------------------------- scaled-down replicas


def _replica_max_nodes():
    """tests/test_parser.py::TestParser::test_max_nodes at a smaller scale.
    The real test parses a 10,000-column query (28.8 s). Node counting is a
    plain per-node counter, so the same statements on a 260-column query
    (~52k nodes) exercise the same code: tokens, trees and error messages."""
    from sqlglot import parse_one
    from sqlglot.serde import dump

    out = []

    def rec(fn):
        try:
            r = fn()
            out.append(["ok", dump(r) if r is not None else None])
        except Exception as e:  # noqa: BLE001
            out.append(["err", type(e).__name__, str(e)])

    rec(lambda: parse_one("SELECT 1", max_nodes=1_000_000))
    cols = [f"col{i}" for i in range(500)]
    rec(lambda: parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000))
    expr = "x" + "=x" * 100
    small = "SELECT " + ",".join(expr for _ in range(20))
    rec(lambda: parse_one(small, max_nodes=1_000_000))
    rec(lambda: parse_one(small, max_nodes=1_500))
    big = "SELECT " + ",".join(expr for _ in range(260))
    rec(lambda: parse_one(big, max_nodes=50_000))
    rec(lambda: parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5))
    rec(lambda: parse_one("SELECT 1, 2, 3"))
    return out


def _exact_optimized_tpch():
    """tests/test_executor.py::TestExecutor::test_optimized_tpch compares duckdb
    results of each TPC-H query and its expected optimized form; sqlglot's only
    part is transpiling both to duckdb. Same SQL text in, same frames out."""
    from sqlglot import transpile

    helpers = importlib.import_module("tests.helpers")
    out = []
    for _, sql, optimized in helpers.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"):
        row = []
        for text in (sql, optimized):
            try:
                row.append(transpile(text, write="duckdb")[0])
            except Exception as e:  # noqa: BLE001
                row.append(f"error {type(e).__name__}: {e}")
        out.append(row)
    return out


REPLICAS = {
    "tests/test_parser.py::TestParser::test_max_nodes": _replica_max_nodes,
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": _exact_optimized_tpch,
}


def replica_fingerprint(test_id: str) -> str:
    import hashlib

    blob = json.dumps(REPLICAS[test_id](), sort_keys=True, default=repr)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _replica_child(root, files, test_id, wfd):
    root = _enter(root, files, pools=False)
    try:
        out = replica_fingerprint(test_id)
    except BaseException:  # noqa: BLE001
        _debug("replica")
        out = None
    os.write(wfd, (json.dumps(out) + "\n").encode())
    os._exit(0)


def replica_check(root: str, files: dict[str, str], test_id: str, timeout: float):
    """Fingerprint of the replica of test_id with files overlaid, or None."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _replica_child(root, files, test_id, wfd)
        finally:
            os._exit(1)
    os.close(wfd)
    buf = _read_until(rfd, pid, timeout)
    try:
        return json.loads(buf.decode().strip().split("\n")[-1])
    except (ValueError, IndexError):
        return None
