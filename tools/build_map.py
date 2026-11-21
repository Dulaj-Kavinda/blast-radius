"""Merge the raw per-worker data from mapplugin.py into the committed map.

usage: python build_map.py <raw dir> <sqlglot checkout> <tests.json> <out dir>

Output layout (all JSON, test sets are hex bitmasks over the order of tests.txt):
  tests.txt                 one test id per line (bit i = line i)
  files.json                {path: hex}      tests that touched the file at all
  opens.json                {path: hex}      tests that opened the file (fixtures, data)
  lines/<path>.json         {"sets": [hex], "lines": {lineno: set index}}
  meta.json                 misc (tests per file / class, tests whose setup ran code)

Expansion rules, applied here so the selector stays simple:
  * code run while importing a test module  -> every test in that module
  * code run in a test's setup / teardown   -> every test in its class (setUpClass
    and tearDownClass errors are reported on every test of the class)
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import coverage


def main() -> None:
    raw, root, tests_json, out = (Path(p) for p in sys.argv[1:5])
    root = root.resolve()
    tests = json.loads(tests_json.read_text())["tests"]
    ids = [t["id"] for t in tests]
    index = {tid: i for i, tid in enumerate(ids)}

    by_module = defaultdict(int)
    by_class = defaultdict(int)
    for i, tid in enumerate(ids):
        by_module[tid.split("::")[0]] |= 1 << i
        by_class["::".join(tid.split("::")[:2])] |= 1 << i

    def expand(ctx: str) -> int:
        kind, _, node = ctx.partition("|")
        if kind == "c":
            return by_module.get(node, 0)
        if kind in ("q", "x"):
            base = node.rsplit("|", 1)[0]
            return 1 << index[base] if base in index else 0
        if node not in index:
            return 0
        if kind == "r":
            return 1 << index[node]
        if kind in ("s", "t"):
            return by_class["::".join(node.split("::")[:2])]
        return 0

    cov = coverage.Coverage(data_file=str(raw / "cov"), config_file=False)
    cov.combine([str(p) for p in raw.glob("cov.*")], keep=True)
    cdata = cov.get_data()

    # per-query coverage of the split tests: test -> file -> line -> query bitmask,
    # and the lines those tests run outside any query
    sub_lines: dict = defaultdict(lambda: defaultdict(dict))
    sub_outside: dict = defaultdict(lambda: defaultdict(set))
    sub_count: dict = defaultdict(int)
    shared_outside: dict = defaultdict(set)
    sub_exec: dict = defaultdict(lambda: defaultdict(set))
    own_outside: dict = defaultdict(lambda: defaultdict(set))

    files_mask: dict[str, int] = {}
    line_maps: dict[str, dict[int, int]] = {}
    for fname in cdata.measured_files():
        rel = os.path.relpath(os.path.realpath(fname), root)
        per_line: dict[int, int] = {}
        for lineno, ctxs in cdata.contexts_by_lineno(fname).items():
            mask = 0
            for c in ctxs:
                mask |= expand(c)
                if c.startswith("q|") or c.startswith("x|"):
                    base, q = c[2:].rsplit("|", 1)
                    if c.startswith("x|"):
                        sub_exec[base][rel].add(lineno)
                    sub_lines[base][rel][lineno] = sub_lines[base][rel].get(lineno, 0) | (1 << int(q))
                    sub_count[base] = max(sub_count[base], int(q) + 1)
            for c in ctxs:
                kind, _, node = c.partition("|")
                if kind in ("s", "t") and node.startswith("tests/test_executor.py::"):
                    shared_outside[rel].add(lineno)  # class setup / teardown
                elif kind == "c" and node == "tests/test_executor.py":
                    shared_outside[rel].add(lineno)  # module import
                elif kind == "r":
                    own_outside[node][rel].add(lineno)  # a split test's own non-query code
            if mask:
                per_line[lineno] = mask
        if per_line:
            line_maps[rel] = per_line
            m = 0
            for v in per_line.values():
                m |= v
            files_mask[rel] = m

    opens: dict[str, int] = defaultdict(int)
    for p in raw.glob("opens.*.json"):
        for ctx, paths in json.loads(p.read_text()).items():
            mask = expand(ctx)
            if ctx in ("boot",) or not mask:
                # opened outside any test: pytest start-up / config files
                for path in paths:
                    opens["@startup@" + path] |= 0
                continue
            for path in paths:
                opens[path] |= mask

    classes: dict[str, int] = defaultdict(int)
    for p in raw.glob("classes.*.json"):
        for ctx, names in json.loads(p.read_text()).items():
            mask = expand(ctx)
            for n in names:
                classes[n] |= mask

    out.mkdir(parents=True, exist_ok=True)
    (out / "classes.json").write_text(json.dumps({k: format(v, "x") for k, v in sorted(classes.items())}))
    (out / "tests.txt").write_text("\n".join(ids) + "\n")
    for base in sub_count:
        for rel, lines in shared_outside.items():
            sub_outside[base][rel] |= lines
        for rel, lines in own_outside.get(base, {}).items():
            sub_outside[base][rel] |= lines
    qtimes = {}
    for p in raw.glob("qtimes.*.json"):
        qtimes.update(json.loads(p.read_text()))
    secs = {t["id"]: t["seconds"] for t in tests}
    subunits = {
        base: {
            "queries": sub_count[base],
            # each query's share of the test's recorded seconds, in ms
            "ms": [
                round(x / max(sum(qtimes[base]), 1e-9) * secs.get(base, 0) * 1000) for x in qtimes.get(base, [])
            ],
            "lines": {f: {str(k): format(v, "x") for k, v in sorted(m.items())} for f, m in sub_lines[base].items()},
            "outside": {f: sorted(v) for f, v in sub_outside[base].items()},
            "exec": {f: sorted(v) for f, v in sub_exec[base].items()},
        }
        for base in sub_count
    }
    (out / "subunits.json").write_text(json.dumps(subunits, separators=(",", ":")))
    (out / "files.json").write_text(json.dumps({k: format(v, "x") for k, v in sorted(files_mask.items())}))
    (out / "opens.json").write_text(json.dumps({k: format(v, "x") for k, v in sorted(opens.items())}))
    lines_dir = out / "lines"
    for rel, per_line in line_maps.items():
        sets: list[str] = []
        set_ix: dict[int, int] = {}
        lines = {}
        for ln, mask in sorted(per_line.items()):
            if mask not in set_ix:
                set_ix[mask] = len(sets)
                sets.append(format(mask, "x"))
            lines[str(ln)] = set_ix[mask]
        dest = lines_dir / (rel + ".json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"sets": sets, "lines": lines}, separators=(",", ":")))
    print(f"{len(ids)} tests, {len(line_maps)} files, {len(opens)} opened paths", file=sys.stderr)


if __name__ == "__main__":
    main()
