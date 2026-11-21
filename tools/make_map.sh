#!/usr/bin/env bash
# Rebuild map/ from scratch: run sqlglot's whole suite once under coverage with
# per-test contexts, then merge. Needs .work/sqlglot (pinned checkout) and
# .work/venv (Python 3.13 + requirements.lock + coverage), see README.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$HERE/.work"
SQ="$WORK/sqlglot"
TESTS_JSON="${TESTS_JSON:-$HERE/kit/sample/tests.json}"

rm -rf "$WORK/mapraw"
cd "$SQ"
env PATH="$WORK/venv/bin:$PATH" SKIP_INTEGRATION=0 MAP_OUT="$WORK/mapraw" MAP_ROOT="$SQ" \
    PYTHONPATH="$SQ:$HERE/tools" \
    python -m pytest tests -q -n "${JOBS:-8}" -p no:cacheprovider -p mapplugin | tail -1
cd "$HERE"
rm -rf map
"$WORK/venv/bin/python" tools/build_map.py "$WORK/mapraw" "$SQ" "$TESTS_JSON" map
"$WORK/venv/bin/python" tools/importtrace.py "$SQ" "$WORK/importtrace.json"
"$WORK/venv/bin/python" tools/build_static.py "$SQ" map "$WORK/venv/bin/python" "$WORK/importtrace.json"
# unpatched TPC fingerprints (runner.tpc_fingerprints), for the selector's cheap check
(cd "$SQ" && env SKIP_INTEGRATION=0 PYTHONPATH="$SQ:$HERE" "$WORK/venv/bin/python" -c "
import json, runner
fp = {t: runner.tpc_fingerprints(t) for t in ('test_execute_tpcds', 'test_execute_tpch')}
fp['replicas'] = {t: runner.replica_fingerprint(t) for t in runner.REPLICAS}
json.dump(fp, open('$HERE/map/tpcfp.json', 'w'))
" 2>/dev/null)
