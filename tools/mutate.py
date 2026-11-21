"""Generate synthetic commits like the sample ones, for local validation only.

usage: python mutate.py <sqlglot checkout> <map dir> <out dir> <count> [seed]

Writes <out>/commits/<id>.patch and <out>/kinds.json. Ground truth (which tests
fail) comes from tools/truth.py, which runs the real suite on each patch.
Kinds: code (operator / constant / argument flips in covered sqlglot lines),
classattr (class-level constants), comment, fixture, test (test file edits),
docstring, multi (2-3 of the others).
"""

import difflib
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
MAP = Path(sys.argv[2])
OUT = Path(sys.argv[3])
COUNT = int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 1)

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
    (r" >= ", " > "), (r" and ", " or "), (r" or ", " and "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r" is not ", " is "), (r" is ", " is not "), (r"\bnot ", ""),
    (r" \+ 1\b", " + 2"), (r" - 1\b", " - 2"), (r"\[0\]", "[1]"), (r"\[1\]", "[0]"),
    (r"\[-1\]", "[0]"), (r", 0\)", ", 1)"), (r"\breturn None\b", "return 0"),
]


def covered_lines(rel):
    p = MAP / "lines" / (rel + ".json")
    if not p.exists():
        return []
    return [int(k) for k in json.loads(p.read_text())["lines"]]


def diff(rel, old, new):
    return "".join(
        difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3
        )
    ).replace(f"--- a/{rel}", f"diff --git a/{rel} b/{rel}\n--- a/{rel}", 1)


src_files = [str(p.relative_to(ROOT)) for p in ROOT.glob("sqlglot/**/*.py")]
src_files = [f for f in src_files if covered_lines(f)]
weights = [len(covered_lines(f)) ** 0.5 for f in src_files]
fixtures = [str(p.relative_to(ROOT)) for p in ROOT.glob("tests/fixtures/**/*.sql")]
test_files = [str(p.relative_to(ROOT)) for p in ROOT.glob("tests/**/test_*.py")]


def mutate_code(rel, class_level=False):
    lines = (ROOT / rel).read_text().splitlines(True)
    cands = covered_lines(rel)
    if class_level:
        cands = [
            i + 1 for i, l in enumerate(lines)
            if re.match(r"^    [A-Z_][A-Z0-9_]* = ", l) or re.match(r'^        "[A-Z_]+": ', l)
        ]
    rng.shuffle(cands)
    for ln in cands[:200]:
        line = lines[ln - 1]
        if line.strip().startswith("#"):
            continue
        swaps = [(a, b) for a, b in SWAPS if re.search(a, line)]
        if class_level and not swaps:
            m = re.search(r"\b(\d+)\b", line)
            if m:
                swaps = [(r"\b" + m.group(1) + r"\b", str(int(m.group(1)) + 1))]
            elif re.search(r'"[A-Z_]+"', line):
                swaps = [(r'"([A-Z_]+)"', r'"\1X"')]
        if not swaps:
            continue
        a, b = rng.choice(swaps)
        new_line = re.sub(a, b, line, count=1)
        if new_line == line:
            continue
        new = lines[:]
        new[ln - 1] = new_line
        return diff(rel, "".join(lines), "".join(new))
    return None


def comment(rel):
    lines = (ROOT / rel).read_text().splitlines(True)
    cands = covered_lines(rel)
    ln = rng.choice(cands)
    line = lines[ln - 1]
    indent = line[: len(line) - len(line.lstrip())]
    text = rng.choice(["# Cheap check first", "# Intentionally not cached", "# NOTE: keep in sync", "# TODO: revisit"])
    new = lines[:]
    if rng.random() < 0.5:
        new.insert(ln - 1, f"{indent}{text}\n")
    else:
        new[ln - 1] = line.rstrip("\n") + f"  {text}\n"
    return diff(rel, "".join(lines), "".join(new))


def docstring(rel):
    lines = (ROOT / rel).read_text().splitlines(True)
    cands = [i for i, l in enumerate(lines) if l.strip().startswith('"""') and len(l.strip()) > 8]
    if not cands:
        return None
    i = rng.choice(cands)
    new = lines[:]
    new[i] = new[i].replace('"""', '"""Note: ', 1)
    return diff(rel, "".join(lines), "".join(new))


def fixture():
    rel = rng.choice(fixtures)
    lines = (ROOT / rel).read_text().splitlines(True)
    cands = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--") and not l.startswith("#")]
    if not cands:
        return None
    i = rng.choice(cands)
    line = lines[i]
    choice = rng.random()
    if choice < 0.4:
        new_line = re.sub(r"\b([a-z])\b", lambda m: m.group(1) + "x", line, count=1)
    elif choice < 0.7:
        new_line = line.replace(" ", "  ", 1) if "'" not in line else line.replace("'", "''", 1)
    else:
        new_line = re.sub(r"\b(\d+)\b", lambda m: str(int(m.group(1)) + 1), line, count=1)
    if new_line == line:
        new_line = line.rstrip("\n") + " AND 1 = 1\n" if "WHERE" in line else line.replace("SELECT", "SELECT DISTINCT", 1)
    if new_line == line:
        return None
    new = lines[:]
    new[i] = new_line
    return diff(rel, "".join(lines), "".join(new))


def test_edit():
    rel = rng.choice(test_files)
    return mutate_code(rel) if rng.random() < 0.7 else comment(rel)


def one(kind):
    if kind == "code":
        return mutate_code(rng.choices(src_files, weights)[0])
    if kind == "classattr":
        return mutate_code(rng.choices(src_files, weights)[0], class_level=True)
    if kind == "comment":
        return comment(rng.choices(src_files, weights)[0])
    if kind == "docstring":
        return docstring(rng.choices(src_files, weights)[0])
    if kind == "fixture":
        return fixture()
    if kind == "test":
        return test_edit()


KINDS = ["code"] * 8 + ["classattr"] * 2 + ["comment"] * 2 + ["fixture"] * 3 + ["test", "docstring", "multi", "multi"]
(OUT / "commits").mkdir(parents=True, exist_ok=True)
kinds = {}
n = 0
while n < COUNT:
    kind = rng.choice(KINDS)
    if kind == "multi":
        parts = [one(k) for k in rng.sample(["code", "comment", "fixture", "classattr", "code"], rng.randint(2, 3))]
        if any(p is None for p in parts):
            continue
        files = [re.search(r"diff --git a/(\S+)", p).group(1) for p in parts]
        if len(set(files)) < len(files):
            continue
        patch = "".join(parts)
    else:
        patch = one(kind)
    if not patch:
        continue
    cid = f"m{n:03d}"
    (OUT / "commits" / f"{cid}.patch").write_text(patch)
    kinds[cid] = kind
    n += 1
(OUT / "kinds.json").write_text(json.dumps(kinds, indent=1))
print(f"wrote {n} commits", file=sys.stderr)
