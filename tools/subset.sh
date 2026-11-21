#!/usr/bin/env bash
# Copy the answered commits of a validation folder into <dir>_done for scoring.
set -euo pipefail
SRC="$1"; DST="${SRC}_done"
rm -rf "$DST"; mkdir -p "$DST/commits"; cp "$SRC/tests.json" "$SRC/answers.json" "$DST/"
python3 -c "import json,sys;print('\n'.join(json.load(open('$SRC/answers.json'))['commits']))" | while read c; do cp "$SRC/commits/$c.patch" "$DST/commits/"; done
echo "$DST"
