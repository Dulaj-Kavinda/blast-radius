#!/usr/bin/env bash
# Board-equivalent score: simulated + CPU_SCALE x local CPU (the scorer's CPU
# measured ~1.85x this machine's). usage: tools/evaluate.sh [data dir]
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(tools/run_local.sh "${1:-}" 2>&1)"
python3 - "$OUT" "${CPU_SCALE:-1.85}" <<'PY'
import re, sys
out, scale = sys.argv[1], float(sys.argv[2])
user = float(re.search(r"user ([\d.]+)", out).group(1)); sys_ = float(re.search(r"sys ([\d.]+)", out).group(1))
sim = float(re.search(r"Simulated: ([\d.]+)", out).group(1)); miss = re.search(r"Misses:\s+(\d+)", out).group(1)
print(f"sim {sim:7.1f}  cpu {user + sys_:5.1f}  board~ {sim + scale * (user + sys_):7.1f}  misses {miss}")
PY
