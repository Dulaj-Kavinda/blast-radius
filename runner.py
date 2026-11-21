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


def _child(root: str, files: dict[str, str], test_ids: list[str], wfd: int) -> None:
    import concurrent.futures
    import unittest

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    sys.meta_path.insert(0, _Overlay(root, files))

    results: dict[str, str] = {}

    def put(tid, verdict):
        if results.get(tid) == "fail":
            return
        results[tid] = verdict
        os.write(wfd, (json.dumps([tid, verdict]) + "\n").encode())

    class Result(unittest.TestResult):
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
            case = getattr(mod, cls_name)(meth)
            case._br_id = tid
            suite.addTest(case)
        result = Result()
        # a test that never reports (killed by the stop) stays "not run"
        suite.run(result)
    except BaseException:  # noqa: BLE001
        # an import error fails every test of that module
        for tid in test_ids:
            if tid not in results:
                put(tid, "fail")
    os._exit(0)


def _import_child(root: str, files: dict[str, str], wfd: int) -> None:
    import concurrent.futures
    import glob
    import pkgutil

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    sys.meta_path.insert(0, _Overlay(root, files))
    failed = []
    try:
        import sqlglot

        names = [i.name for i in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot.")]
    except BaseException:  # noqa: BLE001
        failed.append("sqlglot")
        names = []
    names += sorted(f[:-3].replace("/", ".") for f in glob.glob("tests/**/*.py", recursive=True))
    for name in names:
        if name.endswith("__main__") or name in sys.modules and sys.modules[name] is not None:
            continue
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            failed.append(name)
    os.write(wfd, (json.dumps(failed) + "\n").encode())
    os._exit(0)


def _tpc_child(root, files, test_name, indices, wfd):
    import concurrent.futures

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    sys.meta_path.insert(0, _Overlay(root, files))
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


def verify(root: str, files: dict[str, str], test_ids: list[str], timeout: float) -> dict[str, str]:
    """Run test_ids against root with files overlaid. Returns {id: pass|fail}."""
    if not test_ids:
        return {}
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _child(root, files, test_ids, wfd)
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
