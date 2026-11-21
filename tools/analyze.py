"""How often would "drop the expensive tests when every cheap test that ran the
changed lines passes" miss? Uses ground-truth answers, no test runs.

usage: python analyze.py <data dir with commits/, tests.json, answers.json> [cheap_ms]
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("BR_SRC", str(HERE / ".work" / "sqlglot"))
import selector as S  # noqa: E402

data = Path(sys.argv[1])
cheap_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 1500
tests = json.loads((data / "tests.json").read_text())["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
by_id = {t["id"]: t["number"] for t in tests}
answers = json.loads((data / "answers.json").read_text())["commits"]
ids = S.test_ids()
b2n = [by_id.get(t) for t in ids]


def nums(mask):
    return {b2n[i] for i in range(len(ids)) if mask >> i & 1}


an = S.Analyzer()
risky = ok = 0
saved = 0
for cid, ans in sorted(answers.items()):
    an.reset()
    p = data / "commits" / f"{cid}.patch"
    mask = 0
    for fd in S.parse_patch(p.read_text()):
        mask |= an.file_effect(fd)
    sel, direct = nums(mask), nums(an.direct)
    fail = set(ans["failing"])
    missed_now = fail - sel
    cheap = {n for n in sel if ms[n] <= cheap_ms}
    exp = sel - cheap
    direct_cheap = direct & cheap
    verifiable = an.verifiable and an.patched
    tag = ""
    if missed_now:
        tag = f"MISS NOW {sorted(missed_now)}"
    if verifiable and exp and direct_cheap:
        if fail and not (fail & cheap):
            risky += 1
            tag += f" RISK: only expensive fail {sorted(fail)} (cheap direct {len(direct_cheap)})"
        elif not fail:
            ok += 1
            saved += S.cost_ms(sel, ms)
            tag += f" would save {S.cost_ms(sel, ms) / 1000:.1f}s"
    print(f"{cid} {ans.get('kind', '?'):9s} sel={len(sel):4d} fail={len(fail):3d} exp={len(exp)} {tag}")
print(f"heuristic: {ok} commits saved ({saved / 1000:.1f}s), {risky} would miss")
