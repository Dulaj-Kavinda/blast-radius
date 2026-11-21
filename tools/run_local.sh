#!/usr/bin/env bash
# Run the selector natively (no Docker) on a data folder and score it with the
# kit's score.py. Default data: the 30 sample commits.
#   tools/run_local.sh [data dir with commits/, tests.json, answers.json]
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KIT="$HERE/kit"
DATA="${1:-$KIT/sample}"
OUT="$HERE/.work/output.jsonl"
PY="${PY:-$HERE/.work/venv/bin/python}"

/usr/bin/time -p env BR_DATA="$DATA" BR_SRC="$HERE/.work/sqlglot" BR_MAP="$HERE/map" \
    "$PY" "$HERE/selector.py" > "$OUT" 2> "$HERE/.work/selector.log" || { tail -20 "$HERE/.work/selector.log"; exit 1; }
grep -E "^(real|user|sys)" "$HERE/.work/selector.log" | tr '\n' ' '; echo
python3 "$KIT/score.py" "$DATA" "$DATA/answers.json" "$OUT"
