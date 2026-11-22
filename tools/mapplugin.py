"""pytest plugin used only while building the coverage map (never at run time).

For every test it records, per phase:
  * the lines executed in sqlglot/ and tests/ (coverage.py dynamic contexts)
  * the files opened (audit hook), which catches SQL / JSON fixtures

Context names:  c|<module nodeid>   import of a test module (collection)
                s|<test nodeid>     setup  (setUpClass runs here for the first test)
                r|<test nodeid>     call   (setUp / test body / tearDown)
                t|<test nodeid>     teardown (tearDownClass runs here for the last test)

Output (one set per xdist worker, merged by build_map.py):
  $MAP_OUT/cov.<pid>        coverage data with contexts
  $MAP_OUT/opens.<pid>.json {context: [paths]}
"""

import json
import os
import sys

import coverage
import pytest

OUT = os.environ["MAP_OUT"]
ROOT = os.path.realpath(os.environ["MAP_ROOT"])

_cov = None
_ctx = "boot"
_opens: dict[str, set] = {}


def _audit(event, args):
    if event == "open":
        # Ignore the import system reading modules: only data reads matter here.
        f = sys._getframe(1)
        while f is not None:
            name = f.f_code.co_filename
            if name.startswith("<frozen importlib") or name.endswith("_pytest/assertion/rewrite.py"):
                return
            f = f.f_back
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            try:
                path = os.fsdecode(path)
            except Exception:
                return
            if not os.path.isabs(path):
                path = os.path.join(os.getcwd(), path)
            path = os.path.realpath(path)
            if path.startswith(ROOT + os.sep):
                _opens.setdefault(_ctx, set()).add(os.path.relpath(path, ROOT))


def _switch(name):
    global _ctx
    _ctx = name
    _cov.switch_context(name)


def _roundtrip(obj):
    """Pickle like a real process boundary would (Expression.__reduce__ -> serde)."""
    import pickle

    return pickle.loads(pickle.dumps(obj))


class _InlineExecutor:
    """Stands in for ProcessPoolExecutor so work submitted by tests runs in this
    process, where coverage (and the current test's context) can see it."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
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
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pytest_configure(config):
    global _cov
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    os.makedirs(OUT, exist_ok=True)
    _cov = coverage.Coverage(
        data_file=os.path.join(OUT, f"cov.{os.getpid()}"),
        include=[os.path.join(ROOT, "sqlglot", "*"), os.path.join(ROOT, "tests", "*")],
        config_file=False,
    )
    _cov.start()
    _switch("boot")
    sys.addaudithook(_audit)
    _instrument_classes()


_used: dict[str, set] = {}


def _instrument_classes():
    """Wrap __init__ of every sqlglot class so we learn which classes each test
    instantiates (a class-level change such as WEEK_OFFSET = -2 is read through
    instances, never as an executed line)."""
    import functools
    import importlib
    import pkgutil

    sys.path.insert(0, ROOT)
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        try:
            importlib.import_module(info.name)
        except Exception:
            pass

    seen = set()
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("sqlglot") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if not isinstance(obj, type) or obj in seen:
                continue
            if not getattr(obj, "__module__", "").startswith("sqlglot"):
                continue
            seen.add(obj)
            init = obj.__dict__.get("__init__")
            if init is None or not callable(init) or getattr(init, "_mapwrapped", False):
                continue

            def make(orig):
                @functools.wraps(orig)
                def __init__(self, *a, **k):
                    s = _used.get(_ctx)
                    if s is None:
                        s = _used[_ctx] = set()
                    s.add(type(self))
                    return orig(self, *a, **k)

                __init__._mapwrapped = True
                return __init__

            try:
                setattr(obj, "__init__", make(init))
            except (TypeError, AttributeError):
                pass


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    if isinstance(collector, pytest.Module):
        _switch("c|" + collector.nodeid)
    yield
    if isinstance(collector, pytest.Module):
        _switch("boot")


def _clear_caches():
    """Cross-test caches hide which tests depend on the code that fills them:
    only the first test would be credited. Empty them so each test rebuilds."""
    gen = sys.modules.get("sqlglot.generator")
    if gen is not None and hasattr(gen, "_DISPATCH_CACHE"):
        gen._DISPATCH_CACHE.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _clear_caches()
    _switch("s|" + item.nodeid)
    yield


_cur_test = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global _cur_test
    _cur_test = item.nodeid
    _switch("r|" + item.nodeid)
    yield
    _fx_leave()
    if _fx["test"] == item.nodeid and _fx["times"]:
        _fx_times[item.nodeid] = {str(k): round(v * 1000, 2) for k, v in _fx["times"].items()}
        _fx_times[item.nodeid]["n"] = _fx["k"]
    _cur_test = None


def _per_query_mp_execute(self, schema, tables, sqls, tpch):
    """TestExecutor._mp_execute, one coverage context per TPC query
    (q|<test>|<index>), so a run can later replay only the queries that
    execute a changed line. Same steps as the original: parse in the parent,
    pickle to the worker, execute, pickle back, compare with duckdb."""
    mod = sys.modules[type(self).__module__]
    mod.initializer(*_roundtrip((schema, tables)))
    import time

    tid = _cur_test
    tables_out = []
    spent = [0.0] * len(sqls)
    for i, (args, sql, _) in enumerate(sqls):
        _switch(f"q|{tid}|{i}")
        _clear_caches()
        t0 = time.process_time()
        expr = _roundtrip(mod.parse_one(sql))
        tables_out.append(_roundtrip(mod.mp_execute(expr, _roundtrip(args))))
        spent[i] += time.process_time() - t0
    for i, table in enumerate(tables_out):
        _switch(f"q|{tid}|{i}")
        _clear_caches()
        self.cache.pop(sqls[i][1], None)  # duckdb SQL is transpiled inside this query's context
        t0 = time.process_time()
        if table is not None:
            self.subtestHelper(i, table, tpch=tpch)
        spent[i] += time.process_time() - t0
    _switch(f"r|{tid}")
    _query_times[tid] = spent


_query_times: dict = {}


def _exec_phase(fn):
    """Run fn under x|<test>|<query> while inside a per-query context: the
    executor's data processing, which TPC fingerprints do not cover."""

    def wrapper(*a, **k):
        prev = _ctx
        if prev.startswith("q|"):
            _switch("x|" + prev[2:])
        try:
            return fn(*a, **k)
        finally:
            if prev.startswith("q|"):
                _switch(prev)

    return wrapper


# ------------------------------------------------ per-fixture-entry contexts
# Tests that loop over SQL fixture files (check_file in test_optimizer, identity
# loops) get one context per entry, f|<test>|<k>, k counting every entry the
# fixture loaders yield during the test. check_file defers the comparison to a
# subTest titled after the entry: that phase is mapped back to k by title.
_fx = {"test": None, "k": 0, "titles": {}, "times": {}, "t0": 0.0, "cur": None}
_fx_times: dict = {}


def _fx_enter(k):
    import time

    _fx_leave()
    _switch(f"f|{_fx['test']}|{k}")
    _clear_caches()
    _fx["cur"], _fx["t0"] = k, time.process_time()


def _fx_leave():
    import time

    if _fx["cur"] is not None:
        t = _fx["times"]
        t[_fx["cur"]] = t.get(_fx["cur"], 0.0) + time.process_time() - _fx["t0"]
        _fx["cur"] = None
        _switch(f"r|{_fx['test']}")


def _fx_loader(orig, pairs):
    def loader(filename):
        if _cur_test is None or not _ctx.startswith(("r|", "f|")):
            yield from orig(filename)  # class setup / import: not split
            return
        if _fx["test"] != _cur_test:
            _fx.update(test=_cur_test, k=0, titles={}, times={}, cur=None)
        for i, item in enumerate(orig(filename), start=1):
            k = _fx["k"]
            _fx["k"] += 1
            if pairs:
                meta, sql, _ = item
                title = meta.get("title") or f"{i}, {sql}"
                _fx["titles"][title] = k if title not in _fx["titles"] else None  # ambiguous: outside
            _fx_enter(k)
            yield item
        _fx_leave()

    loader._fxwrapped = True
    return loader


def _install_fx():
    import contextlib
    import unittest

    helpers = sys.modules.get("tests.helpers")
    if helpers is None:
        return
    pairs_orig, single_orig = helpers.load_sql_fixture_pairs, helpers.load_sql_fixtures
    if getattr(pairs_orig, "_fxwrapped", False):
        return
    pairs_w, single_w = _fx_loader(pairs_orig, True), _fx_loader(single_orig, False)
    for name, m in list(sys.modules.items()):
        if not name.startswith("tests") or m is None:
            continue
        if getattr(m, "load_sql_fixture_pairs", None) is pairs_orig:
            m.load_sql_fixture_pairs = pairs_w
        if getattr(m, "load_sql_fixtures", None) is single_orig:
            m.load_sql_fixtures = single_w

    orig_sub = unittest.TestCase.subTest
    sentinel = unittest.case._subtest_msg_sentinel

    @contextlib.contextmanager
    def subTest(self, msg=sentinel, **params):
        k = None
        if isinstance(msg, str) and _fx["test"] == _cur_test and _cur_test is not None and _fx["cur"] is None:
            title = msg[len("(execute) "):] if msg.startswith("(execute) ") else msg
            k = _fx["titles"].get(title)
        if k is None:
            with orig_sub(self, msg, **params):
                yield
            return
        _fx_enter(k)
        try:
            with orig_sub(self, msg, **params):
                yield
        finally:
            _fx_leave()

    unittest.TestCase.subTest = subTest


def pytest_collection_modifyitems(session, config, items):
    _install_fx()
    mod = sys.modules.get("tests.test_executor")
    if mod is not None and hasattr(mod, "TestExecutor"):
        mod.TestExecutor._mp_execute = _per_query_mp_execute
        ex = sys.modules["sqlglot.executor"]
        py = sys.modules["sqlglot.executor.python"]
        py.PythonExecutor.execute = _exec_phase(py.PythonExecutor.execute)
        ex.ensure_tables = _exec_phase(ex.ensure_tables)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _switch("t|" + item.nodeid)
    yield
    _switch("boot")


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    with open(os.path.join(OUT, f"opens.{os.getpid()}.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
    with open(os.path.join(OUT, f"qtimes.{os.getpid()}.json"), "w") as f:
        json.dump(_query_times, f)
    with open(os.path.join(OUT, f"fxtimes.{os.getpid()}.json"), "w") as f:
        json.dump(_fx_times, f)
    classes = {}
    for ctx, types in _used.items():
        names = set()
        for t in types:
            for k in t.__mro__:
                if k.__module__.startswith("sqlglot"):
                    names.add(f"{k.__module__}:{k.__qualname__}")
        classes[ctx] = sorted(names)
    with open(os.path.join(OUT, f"classes.{os.getpid()}.json"), "w") as f:
        json.dump(classes, f)
