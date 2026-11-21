"""Static facts about the pinned sqlglot tree, written next to the coverage map.

usage: python build_static.py <sqlglot checkout> <map dir> <python with sqlglot deps> <importtrace.json>

  names.json   identifier -> tests that executed a statement using it at run time
  static.json  eager modules, module import graph, classes per module,
               import-time uses of names / Class.attr, and a few special tests
  impeffect.json  function line run during import -> tests reached through
               what the import-time statement running it built
"""

import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selector as S  # noqa: E402

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def header_span(st):
    blocks = S._blocks(st)
    first = min((b[0].lineno for b in blocks if b), default=None)
    start = st.lineno
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = min([d.lineno for d in st.decorator_list] + [st.lineno])
    end = (first - 1) if first else st.end_lineno
    return start, end


def names_in(node, a, b):
    out = set()
    for n in ast.walk(node):
        ln = getattr(n, "lineno", None)
        if ln is None or not (a <= ln <= b):
            continue
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and IDENT.match(n.value):
            out.add(n.value)
    return out


def attr_refs(node):
    """(prefix chain, attr) for references like Parser.FUNCTIONS or
    Snowflake.Tokenizer.KEYWORDS (prefix 'Parser', 'Snowflake.Tokenizer')."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            parts = []
            v = n.value
            while isinstance(v, ast.Attribute):
                parts.append(v.attr)
                v = v.value
            if isinstance(v, ast.Name):
                parts.append(v.id)
                out.add((".".join(reversed(parts)), n.attr))
    return out


def imports_with_context(tree):
    """Yield (import node, 'top' | 'func' | 'typing') for every import."""

    def is_type_checking(test):
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, where
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                yield from walk(child, "func" if where == "top" else where)
            elif isinstance(child, ast.If) and is_type_checking(child.test):
                for st in child.body:
                    yield from walk(ast.Module(body=[st], type_ignores=[]), "typing")
                for st in child.orelse:
                    yield from walk(ast.Module(body=[st], type_ignores=[]), where)
            else:
                yield from walk(child, where)

    yield from walk(tree, "top")


def import_time_statements(tree):
    """Yield (stmt, class qualname or '') for statements that run at import:
    module level, class bodies, and module-level if/try blocks."""

    def walk(body, cls):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield st, cls  # decorators / defaults run at import
                continue
            if isinstance(st, ast.ClassDef):
                yield st, cls
                q = f"{cls}.{st.name}" if cls else st.name
                yield from walk(st.body, q)
                continue
            yield st, cls
            for b in S._blocks(st):
                yield from walk(b, cls)

    yield from walk(tree.body, "")


def resolve_import(mod, st, is_pkg):
    out = []
    if isinstance(st, ast.Import):
        for a in st.names:
            out.append(a.name)
    else:
        base = st.module or ""
        if st.level:
            parts = mod.split(".")
            if not is_pkg:
                parts = parts[:-1]
            parts = parts[: len(parts) - (st.level - 1)] if st.level > 1 else parts
            base = ".".join(parts + ([base] if base else []))
        out.append(base)
        for a in st.names:
            out.append(f"{base}.{a.name}")
    return out


def main():
    root, mapdir, py = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), sys.argv[3]
    ids = (mapdir / "tests.txt").read_text().split("\n")[:-1]

    files = sorted(p for p in root.glob("sqlglot/**/*.py")) + sorted(p for p in root.glob("tests/**/*.py"))
    modules = {}
    for p in files:
        rel = str(p.relative_to(root))
        try:
            modules[rel] = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            pass
    known_mods = {S.module_name(r) for r in modules}

    # names -> tests --------------------------------------------------------
    S.MAP = mapdir
    names = defaultdict(int)
    for rel, tree in modules.items():
        lm = S.line_map(rel)
        if not lm:
            continue
        for st in ast.walk(tree):
            if not isinstance(st, ast.stmt):
                continue
            a, b = header_span(st)
            m = 0
            for ln in range(a, b + 1):
                m |= lm.get(ln, 0)
            if not m:
                continue
            for n in names_in(st, a, b):
                names[n] |= m
    sets, set_ix, out_names = [], {}, {}
    for n, m in names.items():
        if m not in set_ix:
            set_ix[m] = len(sets)
            sets.append(format(m, "x"))
        out_names[n] = set_ix[m]
    (mapdir / "names.json").write_text(json.dumps({"sets": sets, "names": out_names}, separators=(",", ":")))

    # import graph, classes, import-time uses --------------------------------
    importers = defaultdict(set)
    module_classes = defaultdict(list)
    module_level_defs = set()
    impuse = defaultdict(list)
    impuse_attr = defaultdict(list)
    lazy_importers = defaultdict(set)
    for rel, tree in modules.items():
        mod = S.module_name(rel)
        is_pkg = rel.endswith("__init__.py")
        for node, where in imports_with_context(tree):
            for target in resolve_import(mod, node, is_pkg):
                if target in known_mods and target != mod:
                    if where == "top":
                        importers[target].add(mod)
                    elif where == "func":
                        lazy_importers[target].add((rel, node.lineno))
        mi = S.Module((root / rel).read_text(encoding="utf-8"))
        for _, _, _, q, _ in mi.classes:
            if "<locals>" not in q:
                module_classes[mod].append(q)
        for st in tree.body:
            module_level_defs |= S.defined_names(st)

    for rel, tree in modules.items():
        if not rel.startswith("sqlglot/"):
            continue
        mod = S.module_name(rel)
        for st, cls in import_time_statements(tree):
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                exprs = list(st.decorator_list)
                if isinstance(st, ast.ClassDef):
                    exprs += st.bases + [k.value for k in st.keywords]
                else:
                    exprs += st.args.defaults + [d for d in st.args.kw_defaults if d]
                used = set()
                attrs = set()
                for e in exprs:
                    used |= S.used_names(e)
                    attrs |= attr_refs(e)
            elif S._blocks(st):
                a, b = header_span(st)
                used = names_in(st, a, b)
                attrs = set()
            else:
                used = S.used_names(st)
                attrs = attr_refs(st)
            dnames = sorted(S.defined_names(st))
            entry = [mod, cls, dnames]
            for n in used:
                if n in module_level_defs:
                    impuse[n].append(entry)
            for prefix, a in attrs:
                impuse_attr[a].append(entry + [prefix])
            if cls:
                for n in used:
                    impuse_attr[n].append(entry + [""])  # bare name in a class body


    # function lines that run during import -> tests reached through what the
    # running import-time statement built
    trace = json.loads(Path(sys.argv[4]).read_text())
    an = S.Analyzer()
    S.SRC = root
    stmt_cache = {}
    stmt_why = {}
    mods_cache = {}

    def stmt_effect(f, ln):
        key = (f, ln)
        if key not in stmt_cache:
            if f not in mods_cache:
                mods_cache[f] = S.Module((root / f).read_text(encoding="utf-8"))
            om = mods_cache[f]
            try:
                stmt_cache[key] = an.import_statement(f, S.module_name(f), om, S.line_map(f), ln, f.startswith("tests/"))
            except Exception as e:  # noqa: BLE001
                print(f"stmt {f}:{ln} failed: {e}", file=sys.stderr)
                stmt_cache[key] = (1 << len(ids)) - 1
            stmt_why[key] = bin(stmt_cache[key]).count("1")
        return stmt_cache[key]

    import_names = set()
    for f, lines in trace.items():
        src_lines = (root / f).read_text(encoding="utf-8").split("\n")
        for ln in lines:
            import_names.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", src_lines[int(ln) - 1]))

    impeffect = {}
    for f, lines in trace.items():
        sets, ix, out_lines = [], {}, {}
        for ln, stmts in lines.items():
            m = 0
            for sf, sl in stmts:
                m |= stmt_effect(sf, sl)
            if m not in ix:
                ix[m] = len(sets)
                sets.append(format(m, "x"))
            out_lines[ln] = ix[m]
        impeffect[f] = {"sets": sets, "lines": out_lines}
    (mapdir / "impeffect.json").write_text(json.dumps(impeffect, separators=(",", ":")))
    (Path(sys.argv[4]).parent / "stmt_effects.json").write_text(
        json.dumps({f"{k[0]}:{k[1]}": v for k, v in sorted(stmt_why.items(), key=lambda kv: -kv[1])}, indent=0)
    )
    all_m = (1 << len(ids)) - 1
    n_all = sum(1 for f in impeffect.values() for v in f["lines"].values() if int(f["sets"][v], 16) == all_m)
    print(f"import-time lines: {sum(len(f['lines']) for f in impeffect.values())}, selecting everything: {n_all}", file=sys.stderr)

    eager = subprocess.run(
        [py, "-c", "import sys, sqlglot; print('\\n'.join(m for m in sys.modules if m.startswith('sqlglot')))"],
        cwd=root,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
        check=True,
    ).stdout.split()

    special = {
        "lazy_load": [t for t in ids if t.endswith("::test_lazy_load")],
        "discovery": [t for t in ids if t.startswith("tests/test_typing.py::") or t.endswith("::test_lazy_load")],
    }

    static = {
        "eager": sorted(eager),
        "importers": {k: sorted(v) for k, v in importers.items()},
        "lazy_importers": {k: sorted(v) for k, v in lazy_importers.items()},
        "module_classes": module_classes,
        "impuse": impuse,
        "impuse_attr": impuse_attr,
        "special": special,
        "import_names": sorted(import_names),
    }
    (mapdir / "static.json").write_text(json.dumps(static, separators=(",", ":")))
    print(
        f"names {len(out_names)}, impuse {len(impuse)}, attr {len(impuse_attr)}, "
        "",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
