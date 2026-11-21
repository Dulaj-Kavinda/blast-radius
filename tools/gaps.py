"""Per-commit cost of the last run (.work/output.jsonl) against the oracle."""
import json, sys
data = sys.argv[1]
t = {x["number"]: round(x["seconds"] * 1000) for x in json.load(open(f"{data}/tests.json"))["tests"]}
a = json.load(open(f"{data}/answers.json"))["commits"]
def cost(ns):
    if not ns: return 0
    s = sum(t[n] for n in ns); mx = max(t[n] for n in ns)
    return min(max(mx, s / k) + 2000 * k for k in range(1, 9))
rows = []
for l in open(".work/output.jsonl"):
    o = json.loads(l); ns = [n for w in o["workers"] for n in w]
    rows.append((cost(ns) - cost(a[o["commit"]]["failing"]), o["commit"], a[o["commit"]].get("kind"), len(ns), cost(ns), cost(a[o["commit"]]["failing"]), len(a[o["commit"]]["failing"])))
for r in sorted(rows, reverse=True)[:15]:
    print(f"{r[1]} {r[2]:9s} sel={r[3]:4d} cost={r[4]/1000:5.1f} oracle={r[5]/1000:5.1f} fail={r[6]}")
print("total", sum(r[4] for r in rows) / 1000, "oracle", sum(r[5] for r in rows) / 1000)
