"""Ground truth for synthetic commits: apply each patch to a scratch copy of
sqlglot, run the whole suite, and record which listed tests fail.

usage: python truth.py <sqlglot checkout> <data dir> [jobs]

Writes <data>/answers.json in the sample format, resuming if it exists.
"Fails" follows SCORING.md: a failure, an error, a failing subtest, a timeout,
or a test file that cannot be imported.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
DATA = Path(sys.argv[2]).resolve()
JOBS = sys.argv[3] if len(sys.argv) > 3 else "8"
HERE = Path(__file__).resolve().parent
VENV = HERE.parent / ".work" / "venv" / "bin"

tests = json.loads((DATA / "tests.json").read_text())["tests"]
by_id = {t["id"]: t["number"] for t in tests}
by_file: dict[str, list[int]] = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])

ans_path = DATA / "answers.json"
answers = json.loads(ans_path.read_text())["commits"] if ans_path.exists() else {}
kinds = json.loads((DATA / "kinds.json").read_text()) if (DATA / "kinds.json").exists() else {}

work = Path(tempfile.mkdtemp(prefix="br-truth-"))
tree = work / "sqlglot"
shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))
subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
subprocess.run(["git", "add", "-A"], cwd=tree, check=True)
subprocess.run(["git", "-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "base"], cwd=tree, check=True)

for patch in sorted((DATA / "commits").glob("*.patch")):
    cid = patch.stem
    if cid in answers:
        continue
    r = subprocess.run(["git", "apply", str(patch)], cwd=tree, capture_output=True, text=True)
    if r.returncode:
        print(f"{cid}: patch does not apply: {r.stderr.strip()}", file=sys.stderr)
        continue
    out = work / f"{cid}.json"
    env = dict(os.environ, PATH=f"{VENV}:{os.environ['PATH']}", SKIP_INTEGRATION="0",
               PYTHONPATH=f"{tree}:{HERE}", TRUTH_OUT=str(out))
    subprocess.run(
        [str(VENV / "python"), "-m", "pytest", "tests", "-q", "-n", JOBS, "-p", "no:cacheprovider",
         "-p", "truthplugin", "--timeout", "300"],
        cwd=tree, env=env, capture_output=True, text=True,
    )
    failing = set()
    if out.exists():
        res = json.loads(out.read_text())
        for nid in res["failed"]:
            if nid in by_id:
                failing.add(by_id[nid])
        for f in res["broken_files"]:
            failing.update(by_file.get(f, []))
    else:
        print(f"{cid}: pytest produced no report; treating every test as failing", file=sys.stderr)
        failing = set(by_id.values())
    answers[cid] = {"failing": sorted(failing), "kind": kinds.get(cid, "?")}
    print(f"{cid}: {len(failing)} failing", file=sys.stderr)
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=tree, check=True)
    subprocess.run(["git", "clean", "-qfd"], cwd=tree, check=True)
    ans_path.write_text(json.dumps({"commits": answers}, indent=1))

shutil.rmtree(work, ignore_errors=True)
