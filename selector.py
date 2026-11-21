"""Blast Radius selector: pick the sqlglot tests a patch can break, then split
them over workers as cheaply as possible.

Standard library only. Inputs:
  $BR_DATA (default /data):   commits/<id>.patch, tests.json
  $BR_MAP  (default <here>/map):  coverage map built by tools/make_map.sh
  $BR_SRC  (default <here>/sqlglot): sqlglot checkout at the pinned commit

How a test is chosen, per changed file (see README.md for the reasoning):
  * Python, comment / formatting only (same token stream)   -> nothing
  * Python, line inside a function body                      -> tests that ran that statement
  * Python, def line / decorator / default argument          -> tests that ran the function
  * Python, class-level statement (WEEK_OFFSET = -2)         -> tests that instantiated the class
                                                                or a subclass, plus name users
  * Python, module-level statement                           -> tests that ran code using the name
  * Python, broken file / import change / deleted module     -> every test that imports the module
  * data file (SQL / JSON fixtures, csv)                     -> tests that opened it
  * anything else (docs, CI, ...)                            -> nothing
Anything unexpected falls back to running every test: a miss costs 300 s.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import time
import tokenize
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BR_DATA", "/data"))
MAP = Path(os.environ.get("BR_MAP", HERE / "map"))
SRC = Path(os.environ.get("BR_SRC", HERE / "sqlglot"))

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def log(*args):
    print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- map


def _hex(s: str) -> int:
    return int(s, 16) if s else 0


@lru_cache(maxsize=None)
def _json(name: str):
    p = MAP / name
    return json.loads(p.read_text()) if p.exists() else {}


@lru_cache(maxsize=None)
def line_map(path: str) -> dict[int, int]:
    p = MAP / "lines" / (path + ".json")
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    sets = [_hex(s) for s in d["sets"]]
    return {int(k): sets[v] for k, v in d["lines"].items()}


@lru_cache(maxsize=None)
def _impeffect(path: str) -> dict[int, int]:
    d = _json("impeffect.json").get(path)
    if not d:
        return {}
    sets = [_hex(s) for s in d["sets"]]
    return {int(k): sets[v] for k, v in d["lines"].items()}


def import_lines_in(path: str, a: int, b: int) -> bool:
    eff = _impeffect(path)
    return any(ln in eff for ln in range(a, b + 1))


def import_effect(path: str, a: int, b: int) -> int:
    """Tests reached through what import-time code built, for lines a..b."""
    eff = _impeffect(path)
    m = 0
    if eff:
        for ln in range(a, b + 1):
            m |= eff.get(ln, 0)
    return m


def opens_mask(path: str) -> int:
    return _hex(_json("opens.json").get(path, ""))


def file_mask(path: str) -> int:
    return _hex(_json("files.json").get(path, ""))


def class_mask(key: str) -> int:
    return _hex(_json("classes.json").get(key, ""))


@lru_cache(maxsize=None)
def _names():
    d = _json("names.json")
    return d.get("sets", []), d.get("names", {})


def names_mask(names) -> int:
    sets, idx = _names()
    m = 0
    for n in names:
        i = idx.get(n)
        if i is not None:
            m |= _hex(sets[i])
    return m


def static() -> dict:
    return _json("static.json")


@lru_cache(maxsize=None)
def import_names() -> frozenset:
    """Identifiers on function lines that run while modules import."""
    return frozenset(static().get("import_names", ()))


@lru_cache(maxsize=None)
def test_ids() -> list[str]:
    return (MAP / "tests.txt").read_text().split("\n")[:-1]


def all_mask() -> int:
    return (1 << len(test_ids())) - 1


@lru_cache(maxsize=None)
def module_tests_mask(prefix: str) -> int:
    """Tests whose id starts with prefix (a test file, or file::Class)."""
    m = 0
    for i, t in enumerate(test_ids()):
        if t == prefix or t.startswith(prefix + "::"):
            m |= 1 << i
    return m


# ------------------------------------------------------------------------ patches


class FileDiff:
    def __init__(self):
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list[tuple[int, list[str]]] = []  # (old_start, lines with prefix)
        self.binary = False
        self.new_file = False
        self.deleted = False

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


def _strip_prefix(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileDiff()
            files.append(cur)
            m = re.match(r"diff --git (\S+) (\S+)$", line)
            if m:
                cur.old_path = _strip_prefix(m.group(1))
                cur.new_path = _strip_prefix(m.group(2))
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FileDiff()
                files.append(cur)
            cur.old_path = _strip_prefix(line[4:])
            cur.new_path = _strip_prefix(lines[i + 1][4:])
            cur.new_file = cur.old_path is None
            cur.deleted = cur.new_path is None
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.new_file = True
            elif line.startswith("deleted file mode"):
                cur.deleted = True
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from ") :].strip()
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to ") :].strip()
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            m = HUNK_RE.match(line)
            if m:
                old_start = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                body = []
                i += 1
                o = n = 0
                while i < len(lines) and (o < old_len or n < new_len):
                    l = lines[i]
                    if l.startswith("\\"):
                        i += 1
                        continue
                    tag = l[:1] if l else " "
                    if tag == "-":
                        o += 1
                    elif tag == "+":
                        n += 1
                    elif tag == " ":
                        o += 1
                        n += 1
                    else:
                        break
                    body.append(l if l else " ")
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                cur.hunks.append((old_start, body))
                continue
        i += 1
    for f in files:
        if f.new_file:
            f.old_path = None
        if f.deleted:
            f.new_path = None
    return files


def apply_hunks(old: str, hunks) -> str:
    old_lines = old.split("\n")
    out: list[str] = []
    pos = 0  # 0-based index into old_lines
    for old_start, body in hunks:
        start = max(old_start - 1, 0) if any(l[:1] in " -" for l in body) else old_start
        out.extend(old_lines[pos:start])
        pos = start
        for l in body:
            tag, text = l[:1], l[1:]
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else text)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(text)
    out.extend(old_lines[pos:])
    return "\n".join(out)


class Change:
    """Old-file view of one hunk: changed old lines and insertion points."""

    def __init__(self):
        self.modified: set[int] = set()  # old lines removed or replaced
        self.structural: set[int] = set()  # old lines whose block structure may change
        self.insert_after: set[int] = set()  # new lines inserted after this old line (0 = top)


def hunk_changes(hunks) -> Change:
    ch = Change()
    for old_start, body in hunks:
        old_no = old_start - 1 if any(l[:1] in " -" for l in body) else old_start
        i = 0
        while i < len(body):
            tag = body[i][:1]
            if tag == " ":
                old_no += 1
                i += 1
                continue
            minus, plus = [], []
            while i < len(body) and body[i][:1] == "-":
                old_no += 1
                minus.append((old_no, body[i][1:]))
                i += 1
            while i < len(body) and body[i][:1] == "+":
                plus.append(body[i][1:])
                i += 1
            before = old_no - len(minus)
            for k, (ln, text) in enumerate(minus):
                ch.modified.add(ln)
                if k >= len(plus) or _indent(plus[k]) != _indent(text) or _opens_block(text) or _opens_block(plus[k]):
                    ch.structural.add(ln)
            if len(plus) > len(minus) or not minus:
                ch.insert_after.add(old_no)
                if any(_opens_block(p) for p in plus):
                    ch.structural.add(old_no)
                    ch.structural.add(old_no + 1)
            if minus and len(plus) < len(minus):
                ch.insert_after.add(before)
    return ch


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip())


def _opens_block(s: str) -> bool:
    code = s.split("#", 1)[0].rstrip()
    return code.endswith(":")


# ---------------------------------------------------------------------- tokens


def _tokens(src: str):
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        out.append((tok.type, tok.string))
    return out


CODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def same_tokens(old: str, new: str) -> bool:
    try:
        if _tokens(old) != _tokens(new):
            return False
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return False
    head_old = [l for l in old.split("\n")[:2] if CODING_RE.match(l)]
    head_new = [l for l in new.split("\n")[:2] if CODING_RE.match(l)]
    return head_old == head_new


# ------------------------------------------------------------------------- AST


class Func:
    __slots__ = ("node", "head_start", "body_start", "end", "cls", "is_lambda")

    def __init__(self, node, head_start, body_start, end, cls, is_lambda):
        self.node, self.head_start, self.body_start, self.end = node, head_start, body_start, end
        self.cls, self.is_lambda = cls, is_lambda


class Module:
    """Scope information for one source file."""

    def __init__(self, src: str):
        self.tree = ast.parse(src)
        self.funcs: list[Func] = []
        self.classes: list[tuple[int, int, int, str, ast.ClassDef]] = []  # head, body, end, qual
        self._walk(self.tree, [], None)

    def _walk(self, node, qual: list[str], cls: str | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                head = min([d.lineno for d in child.decorator_list] + [child.lineno])
                self.funcs.append(Func(child, head, child.body[0].lineno, child.end_lineno, cls, False))
                self._walk(child, qual + [child.name, "<locals>"], None)
            elif isinstance(child, ast.Lambda):
                self.funcs.append(Func(child, child.lineno, child.body.lineno, child.end_lineno, cls, True))
                self._walk(child, qual, cls)
            elif isinstance(child, ast.ClassDef):
                head = min([d.lineno for d in child.decorator_list] + [child.lineno])
                q = ".".join(qual + [child.name])
                self.classes.append((head, child.body[0].lineno, child.end_lineno, q, child))
                self._walk(child, qual + [child.name], q)
            else:
                self._walk(child, qual, cls)

    def func_at(self, line: int) -> Func | None:
        """Innermost function (or lambda) whose header or body holds line."""
        best = None
        for f in self.funcs:
            if f.head_start <= line <= f.end:
                if best is None or (f.head_start, -f.end) >= (best.head_start, -best.end):
                    best = f
        return best

    def class_at(self, line: int):
        best = None
        for c in self.classes:
            if c[0] <= line <= c[2]:
                if best is None or c[0] >= best[0]:
                    best = c
        return best

    def stmt_at(self, line: int, root=None):
        """(innermost statement holding line, its header-only span, parent compound)."""
        parent = None
        body = getattr(root or self.tree, "body", [])
        found = None
        span = None
        while True:
            nxt = None
            for st in _child_stmts(body):
                if st.lineno <= line <= (st.end_lineno or st.lineno):
                    nxt = st
                    break
            if nxt is None:
                break
            blocks = _blocks(nxt)
            first_inner = min((b[0].lineno for b in blocks if b), default=None)
            if not blocks or first_inner is None or line < first_inner:
                found, span = nxt, (nxt.lineno, (first_inner - 1) if first_inner else nxt.end_lineno)
                if isinstance(nxt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    span = (min([d.lineno for d in nxt.decorator_list] + [nxt.lineno]), span[1])
                break
            inner = None
            for b in blocks:
                for st in b:
                    if st.lineno <= line <= (st.end_lineno or st.lineno):
                        inner = b
                        break
                if inner:
                    break
            if inner is None:
                # a line such as `else:` / `finally:` between blocks: header of nxt
                found, span = nxt, (nxt.lineno, first_inner - 1)
                break
            parent = nxt
            body = inner
        return found, span, parent


def _child_stmts(body):
    return [s for s in body if isinstance(s, ast.stmt)]


def _blocks(st):
    out = []
    for name in ("body", "orelse", "finalbody"):
        b = getattr(st, name, None)
        if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
            out.append(b)
    for h in getattr(st, "handlers", []) or []:
        out.append(h.body)
    for c in getattr(st, "cases", []) or []:
        out.append(c.body)
    return out


def defined_names(st) -> set[str]:
    names: set[str] = set()
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(st.name)
        return names
    if isinstance(st, (ast.Import, ast.ImportFrom)):
        for a in st.names:
            names.add((a.asname or a.name).split(".")[0])
        return names
    targets = []
    if isinstance(st, ast.Assign):
        targets = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        targets = [st.target]
    for t in targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    return names


def used_names(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def module_name(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


# --------------------------------------------------------------------- analysis


class Analyzer:
    def __init__(self):
        self._mod_users: dict[str, int] = {}
        self.reset()

    def reset(self):
        """Per-commit facts for run-time verification."""
        self.patched: dict[str, str] = {}  # path -> new source of changed .py files
        self.direct = 0  # tests that ran a changed line itself
        self.verifiable = True  # False: something a test reads cannot be overlaid
        self.import_risk = False  # a changed line runs while modules import
        self.spans: list[tuple[str, int, int]] = []  # line ranges whose coverage selected tests
        self.nonline = 0  # tests selected for any other reason (class users, imports, data...)
        self._path = ""

    # masks over sets of lines ------------------------------------------------
    def span_mask(self, lm: dict[int, int], a: int, b: int) -> int:
        self.spans.append((self._path, a, b))
        m = 0
        for ln in range(a, b + 1):
            m |= lm.get(ln, 0)
        return m

    def func_mask(self, path: str, f: Func) -> int:
        return self.span_mask(line_map(path), f.body_start, f.end)

    # module users -----------------------------------------------------------
    def module_users(self, mod: str, seen=None) -> int:
        """Tests that break if `mod` cannot be imported."""
        if mod in self._mod_users:
            return self._mod_users[mod]
        seen = seen or set()
        if mod in seen:
            return 0
        seen.add(mod)
        st = static()
        if mod in st.get("eager", []) or mod in ("sqlglot", "tests", "tests.helpers"):
            return all_mask()
        m = 0
        path = mod.replace(".", "/") + ".py"
        for cand in (path, mod.replace(".", "/") + "/__init__.py"):
            m |= file_mask(cand)
        for q in st.get("module_classes", {}).get(mod, []):
            m |= class_mask(f"{mod}:{q}")
        if mod.startswith("tests."):
            m |= module_tests_mask(path)
        for imp in st.get("importers", {}).get(mod, []):
            m |= self.module_users(imp, seen)
        for ipath, iline in st.get("lazy_importers", {}).get(mod, []):
            # an import inside a function fails only when that line runs
            m |= line_map(ipath).get(iline, 0) | import_effect(ipath, iline, iline)
        m |= self.special("lazy_load")
        self._mod_users[mod] = m
        return m

    def special(self, key: str) -> int:
        ids = static().get("special", {}).get(key, [])
        m = 0
        idx = {t: i for i, t in enumerate(test_ids())} if ids else {}
        for t in ids:
            if t in idx:
                m |= 1 << idx[t]
        return m

    # import-time propagation ------------------------------------------------
    def name_users(self, names: set[str], cls_names: set[str]) -> int:
        """Tests reached through names defined by an import-time statement,
        following class bodies / module constants that copy them."""
        imp = static().get("impuse", {})
        m = names_mask(names)
        frontier = set(names)
        seen = set(frontier)
        for _ in range(4):
            nxt = set()
            for n in frontier:
                for mod, cq, dnames in imp.get(n, []):
                    if cq:
                        m |= class_mask(f"{mod}:{cq}")
                    for d in dnames:
                        if d not in seen:
                            seen.add(d)
                            nxt.add(d)
            if not nxt:
                break
            m |= names_mask(nxt)
            frontier = nxt
        return m

    # one python file ---------------------------------------------------------
    def python_file(self, fd: FileDiff) -> int:
        path = fd.path
        mod = module_name(path)
        self._path = path
        old_path = SRC / fd.old_path if fd.old_path else None
        if fd.deleted or (fd.old_path and fd.new_path and fd.old_path != fd.new_path):
            # module removed or renamed away: everything importing it breaks
            self.verifiable = False
            return self._nl(self.module_users(mod) | opens_mask(path))
        if fd.new_file or old_path is None or not old_path.exists():
            # a brand-new module: only code that discovers modules can see it
            self.verifiable = False
            return self._nl(self.special("discovery") | self.special("lazy_load"))
        old = old_path.read_text(encoding="utf-8")
        new = apply_hunks(old, fd.hunks)
        mask = self._nl(opens_mask(path))  # tests that read this source as text
        if mask:
            self.verifiable = False
        if same_tokens(old, new):
            return mask
        try:
            ast.parse(new)
        except (SyntaxError, ValueError):
            self.verifiable = False
            return mask | self._nl(self.module_users(mod))
        self.patched[path] = new
        om = Module(old)
        lm = line_map(path)
        ch = hunk_changes(fd.hunks)
        is_test = path.startswith("tests/")

        lines = set(ch.modified)
        for a in ch.insert_after:
            lines.add(max(a, 1))
            lines.add(a + 1)
        for ln in lines:
            self.direct |= lm.get(ln, 0)
        for ln in sorted(lines):
            mask |= self.line_effect(path, mod, om, lm, ln, ln in ch.structural or ln not in ch.modified, is_test)
        return mask

    def line_effect(self, path, mod, om: Module, lm, ln, structural, is_test) -> int:
        f = om.func_at(ln)
        if f is not None and (f.is_lambda or ln >= f.body_start):
            return self.in_function(path, om, lm, f, ln, structural)
        if f is not None:
            # def line, decorator or default argument: every caller
            self.import_risk = True
            m = self.func_mask(path, f) | self._nl(import_effect(path, f.body_start, f.end))
            if is_test:
                m |= self._nl(self.test_func_owner(path, om, f))
            return m
        return self._nl(self.import_statement(path, mod, om, lm, ln, is_test))

    def _nl(self, m: int) -> int:
        self.nonline |= m
        return m

    def test_func_owner(self, path, om, f: Func) -> int:
        """A test method's own header: that test (renamed, decorated, ...)."""
        c = om.class_at(f.head_start)
        name = getattr(f.node, "name", "")
        if c is not None:
            return module_tests_mask(f"{path}::{c[3]}::{name}")
        return module_tests_mask(f"{path}::{name}")

    def in_function(self, path, om, lm, f: Func, ln, structural) -> int:
        if f.is_lambda:
            a, b = f.head_start, f.end
            m = self.span_mask(lm, a, b)
        else:
            st, span, parent = om.stmt_at(ln, f.node)
            if st is None:
                # blank / comment line inside a body: neighbours decide
                a, b = max(f.body_start, ln - 1), min(f.end, ln + 1)
                m = self.span_mask(lm, a, b)
                if structural:
                    a, b = f.body_start, f.end
                    m |= self.func_mask(path, f)
            else:
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                    if not structural:
                        return 0  # docstring
                a, b = span
                m = self.span_mask(lm, a, b)
                if structural:
                    if parent is not None and parent is not f.node:
                        _, pspan, _ = om.stmt_at(parent.lineno, f.node)
                        if pspan:
                            m |= self.span_mask(lm, pspan[0], pspan[1])
                            m |= self._nl(import_effect(path, pspan[0], pspan[1]))
                    else:
                        a, b = f.body_start, f.end
                        m |= self.func_mask(path, f)
        # the same lines may also run while modules import (templates, factories)
        if import_lines_in(path, a, b):
            self.import_risk = True
        return m | self._nl(import_effect(path, a, b))

    def import_statement(self, path, mod, om: Module, lm, ln, is_test) -> int:
        """A module- or class-level statement (runs once, at import)."""
        self.import_risk = True
        st, span, parent = om.stmt_at(ln)
        if st is None:
            return 0
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            return 0  # docstring
        c = om.class_at(ln)
        if is_test:
            return self.test_import_statement(path, mod, om, st, c)
        if isinstance(st, (ast.Import, ast.ImportFrom)):
            return self.module_users(mod) | names_mask(defined_names(st))
        names = defined_names(st)
        m = self.span_mask(lm, span[0], span[1])
        if c is not None:
            cq = c[3]
            const = isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and bool(names)
            m |= self.class_attr_effect(mod, cq, names, const, set())
        else:
            if not names:
                # a bare call / if-block at module level: anything importing it
                return m | self.module_users(mod)
            m |= self.name_users(names, set())
            if isinstance(st, ast.ClassDef):
                m |= class_mask(f"{mod}:{st.name}")
        return m

    def class_attr_effect(self, mod: str, cq: str, names: set[str], const: bool, seen: set) -> int:
        """Tests affected when class attributes `names` of class cq change.

        A constant read only at run time (self.WEEK_OFFSET) matters only to tests
        that use the class and ran a line mentioning it. If import-time code
        (a metaclass building tries, a class body copying it) reads the name,
        everything built from the class may differ: every user of the class.
        """
        key = (mod, cq, frozenset(names))
        if key in seen:
            return 0
        seen.add(key)
        cm = class_mask(f"{mod}:{cq}")
        if not cm:
            # never instantiated (enum, namespace): whoever names it
            m = names_mask(names | {cq.split(".")[-1]})
        elif const and not (names & import_names()):
            m = names_mask(names) & cm
        else:
            m = cm
        # class bodies that copy the attribute (FUNCTIONS = {**Parser.FUNCTIONS})
        imp = static().get("impuse_attr", {})
        q = cq.split(".")
        outer = q[-2] if len(q) > 1 else mod.split(".")[-1]
        for a in names:
            for emod, ecq, dnames, prefix in imp.get(a, []):
                if prefix == "":
                    hit = emod == mod and ecq == cq  # same class body, e.g. B = A | {...}
                else:
                    parts = prefix.split(".")
                    hit = parts[-1] == q[-1] and (
                        (len(parts) > 1 and parts[-2] == outer) or (len(parts) == 1 and (emod == mod or len(q) == 1))
                    )
                if not hit:
                    continue
                if ecq:
                    m |= self.class_attr_effect(emod, ecq, set(dnames), bool(dnames), seen)
                else:
                    m |= self.name_users(set(dnames), set())
        return m

    def test_import_statement(self, path, mod, om, st, c) -> int:
        """Module / class level code in a test file."""
        importers = static().get("importers", {}).get(mod, [])
        if c is not None and not importers:
            return module_tests_mask(f"{path}::{c[3].split('.')[0]}")
        return self.module_users(mod) if importers else module_tests_mask(path)

    # any file -----------------------------------------------------------------
    def file_effect(self, fd: FileDiff) -> int:
        m = self._file_effect(fd)
        if not (fd.path.endswith(".py") and (fd.path.startswith("sqlglot/") or fd.path.startswith("tests/"))):
            self.nonline |= m
        return m

    def _file_effect(self, fd: FileDiff) -> int:
        path = fd.path
        newp = fd.new_path or ""
        if fd.old_path and fd.new_path and fd.old_path != fd.new_path and not path.endswith(".py"):
            return opens_mask(fd.old_path) | opens_mask(fd.new_path) | self.file_effect_simple(fd.new_path, fd)
        if path.endswith(".py") and (path.startswith("sqlglot/") or path.startswith("tests/")):
            base = os.path.basename(path)
            if base == "conftest.py":
                self.verifiable = False
                return self._nl(all_mask())
            if path.startswith("tests/") and fd.new_file:
                return 0  # a new test module holds no listed tests
            return self.python_file(fd)
        return self.file_effect_simple(path, fd) | (self.file_effect_simple(newp, fd) if newp != path else 0)

    def file_effect_simple(self, path: str, fd: FileDiff) -> int:
        base = os.path.basename(path)
        if base in ("conftest.py", "pytest.ini", "tox.ini") or (
            base in ("pyproject.toml", "setup.cfg") and _mentions_pytest(fd)
        ):
            self.verifiable = False
            return all_mask()
        m = opens_mask(path)
        if m and path.endswith(".sql") and not fd.binary and _sql_comment_only(fd):
            return 0
        if m:
            self.verifiable = False  # a data file the tests read: cannot overlay it
        if fd.deleted and m:
            return m
        return m


def _mentions_pytest(fd: FileDiff) -> bool:
    return any("pytest" in l for _, body in fd.hunks for l in body)


def _sql_comment_only(fd: FileDiff) -> bool:
    """Fixture loaders drop empty lines and lines starting with --."""
    for _, body in fd.hunks:
        for l in body:
            if l[:1] in "+-":
                t = l[1:]
                if t and not t.startswith("--"):
                    return False
    return True


# ----------------------------------------------------------------------- split


def split(numbers: list[int], ms: dict[int, int]) -> list[list[int]]:
    """Choose worker count and assignment minimising max load + 2 s per worker."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -ms[n])
    total = sum(ms[n] for n in numbers)
    best = None
    for k in range(1, MAX_WORKERS + 1):
        if k > len(order):
            break
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += ms[n]
            bins[j].append(n)
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
        if max(ms[order[0]], total / (k + 1)) + WORKER_COST_MS * (k + 1) >= best[0] and k > 1:
            # more workers cannot beat the best so far
            break
    return [b for b in best[1] if b]


# ------------------------------------------------------------------------ main


def _queries_for(sub: dict, spans) -> int | None:
    """Bitmask of the queries that ran any line in spans; None if one of those
    lines also runs outside the queries."""
    q = 0
    for path, a, b in spans:
        lines = sub["lines"].get(path, {})
        outside = sub["outside"].get(path)
        for ln in range(a, b + 1):
            if outside and ln in outside:
                return None
            h = lines.get(str(ln))
            if h:
                q |= int(h, 16)
    return q


def cost_ms(nums, ms) -> int:
    workers = split(list(nums), ms)
    return (max(sum(ms[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)) if workers else 0


# Tests whose outcome a forked, in-memory run cannot reproduce faithfully.
UNVERIFIABLE_RE = re.compile(r"::test_lazy_load$|^tests/test_sqlglotc\.py|::test_generator_memory$")
VERIFY_MAX_TEST_MS = int(os.environ.get("BR_VERIFY_MAX_TEST_MS", "4000"))
VERIFY_OVERHEAD_MS = 250  # fork + fresh sqlglot / test imports, measured ~100-200 ms
VERIFY_BUDGET_S = float(os.environ.get("BR_VERIFY_BUDGET_S", "75"))
# Never let checks push the whole batch near the run limit (3 min, 4.5 in the final).
DEADLINE_S = float(os.environ.get("BR_DEADLINE_S", "110"))
START = time.monotonic()


def time_left() -> bool:
    return time.monotonic() - START < DEADLINE_S


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ids = test_ids()
    bit_to_number = [by_id.get(t) for t in ids]
    mapped = {n for n in bit_to_number if n is not None}
    unmapped = [t["number"] for t in tests if t["number"] not in mapped]
    if unmapped:
        log(f"warning: {len(unmapped)} tests are not in the map; they run on every non-empty commit")

    def to_numbers(mask):
        return {bit_to_number[i] for i in range(len(ids)) if mask >> i & 1 and bit_to_number[i] is not None}

    an = Analyzer()
    plans = []  # (commit, selected numbers, patched files or None, direct numbers)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        commit = patch.stem
        an.reset()
        try:
            text = patch.read_text(encoding="utf-8", errors="replace")
            files = parse_patch(text)
            mask = 0
            for fd in files:
                mask |= an.file_effect(fd)
            if not files and text.strip():
                log(f"{commit}: could not read the patch; running everything")
                mask = all_mask()
                an.verifiable = False
        except Exception as e:  # never risk a miss on a crash
            log(f"{commit}: analysis failed ({type(e).__name__}: {e}); running everything")
            mask = all_mask()
            an.verifiable = False
        nums = to_numbers(mask)
        if mask and unmapped:
            nums.update(unmapped)
        patched = dict(an.patched) if an.verifiable and an.patched else None
        risky = dict(an.patched) if an.import_risk and an.patched else None
        plans.append([commit, nums, patched, to_numbers(an.direct), risky, list(an.spans), to_numbers(an.nonline)])

    # Run-time verification: for commits where running the cheap selected tests
    # is expected to cost less than listing them, run them and drop the ones
    # that pass. A test that fails, or that was not run, stays selected.
    verify_on = os.environ.get("BR_VERIFY", "1") == "1" and SRC.exists()
    runner = None
    if verify_on and any(p[2] or p[4] for p in plans):
        sys.path.insert(0, str(HERE))
        import runner as _runner

        runner = _runner if _runner.preload() else None
        verify_on = runner is not None

    # Import check: a changed line that runs while modules import can raise
    # there, and then every test importing that module fails. Importing every
    # sqlglot and test module with the patch applied tells exactly which break.
    risky_plans = [p for p in plans if p[4]]
    if risky_plans:
        base = runner.import_check(str(SRC), {}) if runner else None
        for plan in risky_plans:
            failed = (
                runner.import_check(str(SRC), plan[4]) if runner and base is not None and time_left() else None
            )
            if failed is None:
                log(f"{plan[0]}: import check unavailable; selecting importers of the changed modules")
                extra = 0
                for path in plan[4]:
                    extra |= an.module_users(module_name(path))
            else:
                new = [m for m in failed if m not in base]
                extra = 0
                for m in new:
                    extra |= an.module_users(m)
                if new:
                    log(f"{plan[0]}: import fails for {new[:5]}{'...' if len(new) > 5 else ''}")
                    plan[2] = None  # most tests fail anyway: nothing to gain from verifying
            plan[1] = set(plan[1]) | to_numbers(extra)
            plan[6] = set(plan[6]) | to_numbers(extra)

    if verify_on:
        candidates = []
        for plan in plans:
            commit, nums, patched, direct = plan[:4]
            if not patched or not nums:
                continue
            runnable = [n for n in nums if ms[n] <= VERIFY_MAX_TEST_MS and not UNVERIFIABLE_RE.search(id_of[n])]
            if not runnable:
                continue
            run_ms = sum(ms[n] for n in runnable) + VERIFY_OVERHEAD_MS
            if any(id_of[n].startswith("tests/test_executor.py::TestExecutor::") for n in runnable):
                run_ms += 900  # setUpClass loads the TPC-H / TPC-DS tables
            gain = cost_ms(nums, ms) - cost_ms(set(nums) - set(runnable), ms) - run_ms
            if gain > 0:
                # likeliest failures first: tests that ran the changed lines, cheapest first
                order = sorted(runnable, key=lambda n: (n not in direct, ms[n]))
                candidates.append((gain, plan, order, run_ms))
        spent = 0.0
        for gain, plan, order, run_ms in sorted(candidates, key=lambda c: -c[0]) if verify_on else []:
            if spent + run_ms / 1000 > VERIFY_BUDGET_S or not time_left():
                continue
            t0 = time.monotonic()
            res = runner.verify(str(SRC), plan[2], [id_of[n] for n in order], timeout=3 * run_ms / 1000 + 3)
            spent += time.monotonic() - t0
            passed = {by_id[t] for t, v in res.items() if v == "pass"}
            failed = [t for t, v in res.items() if v == "fail"]
            plan[1] = set(plan[1]) - passed
            log(f"{plan[0]}: verified {len(res)}/{len(order)} tests in {time.monotonic() - t0:.2f}s, "
                f"{len(failed)} failing, {len(passed)} dropped")
        log(f"verification wall time {spent:.1f}s")

        # Split TPC tests: replay only the queries that run a changed line.
        subunits = _json("subunits.json")
        for sub in subunits.values():
            sub["outside"] = {k: set(v) for k, v in sub["outside"].items()}
            sub["exec"] = {k: set(v) for k, v in sub.get("exec", {}).items()}
        base_fps = _json("tpcfp.json")
        for plan in plans:
            commit, nums, patched = plan[0], plan[1], plan[2]
            if not patched:
                continue
            for tid, sub in subunits.items():
                n = by_id.get(tid)
                if n is None or n not in nums or n in plan[6]:
                    continue
                q = _queries_for(sub, plan[5])
                if q is None:
                    continue  # a changed line runs outside the queries (setup, pool, import)
                indices = [i for i in range(sub["queries"]) if q >> i & 1]
                qms = sub.get("ms") or [ms[n] // max(sub["queries"], 1)] * sub["queries"]
                est_ms = 900 + sum(qms[i] for i in indices)
                save_ms = cost_ms(nums, ms) - cost_ms(set(nums) - {n}, ms)
                if not indices:
                    plan[1] = set(nums) - {n}
                    log(f"{commit}: {tid.split('::')[-1]} runs no changed line in any query; dropped")
                    nums = plan[1]
                    continue
                name = tid.split("::")[-1]
                # Cheap check first: if the patch leaves the executor's own code alone
                # and every query still parses, optimizes, plans and transpiles to the
                # same thing, the executor sees identical input: the test cannot change.
                exec_hit = any(p.startswith(("sqlglot/executor/", "tests/")) for p in patched) or any(
                    ln in sub["exec"].get(path, ())
                    for path, a, b in plan[5]
                    for ln in range(a, b + 1)
                )
                if not exec_hit and name in base_fps and time_left():
                    t0 = time.monotonic()
                    fps = runner.tpc_fingerprint_check(str(SRC), patched, name, indices, timeout=30)
                    spent += time.monotonic() - t0
                    if fps is not None and len(fps) == len(indices):
                        changed = [i for i, h in zip(indices, fps) if h != base_fps[name][i]]
                        log(f"{commit}: {name} fingerprints of {len(indices)} queries in "
                            f"{time.monotonic() - t0:.2f}s: {len(changed)} changed")
                        if not changed:
                            plan[1] = set(nums) - {n}
                            nums = plan[1]
                            continue
                        indices = changed
                        est_ms = 900 + sum(qms[i] for i in indices)
                if est_ms + 1000 >= save_ms or spent + est_ms / 1000 > VERIFY_BUDGET_S or not time_left():
                    continue
                t0 = time.monotonic()
                verdict = runner.run_tpc_queries(
                    str(SRC), patched, tid.split("::")[-1], indices, timeout=3 * est_ms / 1000 + 5
                )
                spent += time.monotonic() - t0
                log(f"{commit}: {tid.split('::')[-1]} replayed {len(indices)} queries in "
                    f"{time.monotonic() - t0:.2f}s: {verdict}")
                if verdict == "pass":
                    plan[1] = set(nums) - {n}
                    nums = plan[1]

    # Scaled-down replicas of single huge tests (test_max_nodes, 28.8 s): if the
    # patched code gives the replica the same tokens, trees and errors as the
    # pinned code, the full-size test behaves the same.
    replica_fps = _json("tpcfp.json").get("replicas", {}) if runner else {}
    for plan in plans if runner else []:
        commit, nums, patched = plan[0], plan[1], plan[2]
        if not patched or any(p.startswith("tests/") for p in patched):
            continue
        for tid, base in replica_fps.items():
            n = by_id.get(tid)
            if n is None or n not in nums or not time_left():
                continue
            if cost_ms(nums, ms) - cost_ms(set(nums) - {n}, ms) < 1500:
                continue
            t0 = time.monotonic()
            fp = runner.replica_check(str(SRC), patched, tid, timeout=20)
            log(f"{commit}: {tid.split('::')[-1]} replica in {time.monotonic() - t0:.2f}s: "
                f"{'same' if fp == base else 'changed' if fp else 'unavailable'}")
            if fp == base:
                plan[1] = set(nums) - {n}
                nums = plan[1]

    total_cost = 0
    for commit, nums, *_ in plans:
        nums = sorted(nums)
        workers = split(nums, ms)
        cost = (max(sum(ms[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)) if workers else 0
        total_cost += cost
        log(f"{commit}: {len(nums)} tests, {len(workers)} workers, {cost / 1000:.1f}s")
        print(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    log(f"estimated simulated total without misses: {total_cost / 1000:.1f}s")


if __name__ == "__main__":
    main()
