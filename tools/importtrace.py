"""Record which import-time statement makes each function line run during import.

usage: python importtrace.py <sqlglot checkout> <out.json>

sqlglot builds things while it imports (SQL templates parsed in class bodies,
TRANSFORMS built by helper factories, metaclasses...). A change to a function
line that runs then alters whatever that statement built. Output:
  {func_file: {func_line: [[stmt_file, stmt_line], ...]}}
where stmt_* is the module-level or class-body line that was executing.
"""

import ast
import importlib
import json
import os
import pkgutil
import sys
import threading
from collections import defaultdict

ROOT = os.path.realpath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
PKG = os.path.join(ROOT, "sqlglot") + os.sep
TESTS = os.path.join(ROOT, "tests") + os.sep
sys.path.insert(0, ROOT)

class_starts = defaultdict(set)  # file -> first lines of class bodies' code objects
for dirpath, _, names in [w for d in (PKG, TESTS) for w in os.walk(d)]:
    for n in names:
        if n.endswith(".py"):
            p = os.path.join(dirpath, n)
            try:
                tree = ast.parse(open(p, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    # a class body's code object starts at the first decorator
                    class_starts[p].add(min([d.lineno for d in node.decorator_list] + [node.lineno]))

records = defaultdict(set)
stack = []  # [file, current line] of import-time frames


def is_import_frame(code):
    if code.co_name == "<module>":
        return True
    return code.co_firstlineno in class_starts.get(code.co_filename, ())


def local_import(frame, event, arg):
    if event == "line":
        stack[-1][1] = frame.f_lineno
    elif event == "return":
        stack.pop()
    return local_import


def local_func(frame, event, arg):
    if event == "line" and stack:
        top = stack[-1]
        records[(frame.f_code.co_filename, frame.f_lineno)].add((top[0], top[1]))
    return local_func


def global_trace(frame, event, arg):
    code = frame.f_code
    fn = code.co_filename
    if not (fn.startswith(PKG) or fn.startswith(TESTS)):
        return None
    if is_import_frame(code):
        stack.append([fn, frame.f_lineno])
        return local_import
    if stack:
        top = stack[-1]
        records[(fn, frame.f_lineno)].add((top[0], top[1]))
        return local_func
    return None


sys.settrace(global_trace)
threading.settrace(global_trace)
import sqlglot  # noqa: E402

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except Exception as e:  # noqa: BLE001
        print(f"skip {info.name}: {e}", file=sys.stderr)
os.environ["SKIP_INTEGRATION"] = "0"
os.chdir(ROOT)
for dirpath, _, names in os.walk(TESTS):
    for n in sorted(names):
        if n.endswith(".py"):
            mod = os.path.relpath(os.path.join(dirpath, n), ROOT)[:-3].replace(os.sep, ".")
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: BLE001
                print(f"skip {mod}: {e}", file=sys.stderr)
sys.settrace(None)

out = defaultdict(dict)
for (fn, ln), stmts in records.items():
    rel = os.path.relpath(fn, ROOT)
    out[rel][str(ln)] = sorted([os.path.relpath(f, ROOT), l] for f, l in stmts)
with open(OUT, "w") as f:
    json.dump(out, f)
print(f"{sum(len(v) for v in out.values())} function lines run at import", file=sys.stderr)
