"""pytest plugin for truth.py: record every failing test id (failures, errors,
failing subtests) and test files that failed to import."""

import json
import os

_failed = set()
_broken = set()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid.split("[")[0] if "::" in report.nodeid else report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _broken.add(report.nodeid.split("::")[0])


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    with open(os.environ["TRUTH_OUT"], "w") as f:
        json.dump({"failed": sorted(_failed), "broken_files": sorted(_broken)}, f)
