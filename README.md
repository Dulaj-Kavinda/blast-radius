# Blast Radius: coverage-map test selector

For each patch to sqlglot (pinned at `eb11aec`), this picks the tests that can
fail and splits them over workers at the lowest cost. Nothing is run at score
time except a few cheap checks that pay for themselves (below). On the 30
sample commits: 94.4 s simulated (exactly the oracle), 0 misses, about 15 s of
local CPU. On 66
synthetic commits checked against real test runs: 0 misses. First practice
submission (before the TPC fingerprints): 199.6 s simulated + 8.0 s CPU = 207.6 s.

## How it decides

The map in `map/` is built once by running sqlglot's whole suite under
coverage, with a separate context for each test and each phase:

| Recorded per test | Used for |
| --- | --- |
| Lines executed in `sqlglot/` and `tests/` | changes inside function bodies |
| Files opened (audit hook) | SQL / JSON fixtures, TPC-H / TPC-DS data, source read as text |
| Classes instantiated (wrapped `__init__`, whole MRO) | class-level changes like `WEEK_OFFSET = -2` |
| Lines run while importing a test module, or in `setUpClass` / `tearDownClass` | widened to the whole module or class, because an error there fails all of its tests |

Process pools in the tests are swapped for an in-process executor that still
pickles arguments and results, so code run in workers (and `serde`, used
through `__reduce__`) is attributed to the test that submitted it.

A trace of `import sqlglot` (`tools/importtrace.py`) records which
module-level or class-body statement makes each function line run during
import. sqlglot parses SQL templates and builds `TRANSFORMS` while importing,
so a change to `_parse_case` can also change a template. The test set for
such a line includes whoever uses what that statement built.

`selector.py` then classifies every changed file:

| Change | Tests selected |
| --- | --- |
| Python, same token stream (comments, blank lines, reformatting) | none, except tests that read that source as text |
| Line inside a function body | tests that ran that statement; if indentation or block structure changed, tests that reached the enclosing block |
| `def` line, decorator, default argument | every test that ran the function |
| Class-level statement | tests that instantiated the class or a subclass, plus class bodies that copy the attribute (`{**Parser.FUNCTIONS}`) |
| Module-level statement | tests that ran code using the names it defines, followed through constants that copy them |
| Import change, syntax error, deleted module | every test that imports the module (for eagerly imported modules, all tests) |
| Docstring | none |
| Data file | tests that opened it. Changes that only touch `--` comment or blank lines in `.sql` fixtures select none, because the fixture loaders drop those lines. |
| `conftest.py`, pytest config | all tests |
| Anything else (docs, CI, a new test file) | none |

Any exception during analysis falls back to running every test, because a miss
costs 300 s and running everything costs about 45 s.

### Run-time checks (CPU is charged, so only when they pay)

`runner.py` forks children from a parent that preloaded duckdb, pandas and
numpy. Each child serves the patched `.py` files from memory (the disk is
read-only), imports sqlglot fresh and runs tests with `unittest`. Process
pools are replaced by an in-process executor that still pickles.

1. **Import check.** When a changed line runs while modules import (sqlglot
   parses SQL templates in class bodies), the child imports every sqlglot
   and test module with the patch applied. A module that now fails to import
   adds every test that imports it. This caught synthetic patches that break
   about 1,200 tests through `DuckDBGenerator`'s templates.
2. **Cheap verification.** When running the selected tests up to 1.5 s each
   is expected to cost less than listing them, they run cheapest first,
   starting with tests that executed the changed lines, and stop at the first
   failure. Tests that pass are dropped. Tests that fail, time out or do not
   run stay selected.
3. **TPC query replay.** The map records per-query coverage and CPU time for
   `test_execute_tpcds` (21.5 s, 99 queries) and `test_execute_tpch`. If one
   was selected only because it runs the changed lines, and the replay is
   cheap enough, only the queries that execute those lines are replayed. If
   they all pass, the test is dropped.

4. **TPC fingerprints.** Before replaying, the child fingerprints each affected
   query: the parsed and pickled expression, the tables it reads, the optimized
   expression, the full plan, and the DuckDB SQL the expected result comes
   from. This takes about 1.3 s for all 57 executed TPC-DS queries. If the
   patch does not touch code that runs during execution (recorded per query in
   the map) and no fingerprint changed, the executor gets identical input and
   the test is dropped without running anything. Otherwise, only the queries
   whose fingerprints changed are replayed.

5. **Measured CPU, not recorded seconds.** `tools/measure_cpu.py` records
   what each test really costs in a warm forked child (`map/cpu.json`).
   Recorded seconds include pytest and process-pool start-up: in total
   103 s recorded against 36 s measured, and `test_execute_tpch` 3.9 s against
   0.03 s. The selector times a small benchmark at start-up to scale these
   costs to the scoring machine. It then runs every selected test that is
   cheaper to run than to list, without stopping at the first failure, and
   keeps only the tests that fail. It stops after 25 failures.
6. **`is` ↔ `==` refactors.** When every change is a swap between `is` and
   `==` (or `is not` and `!=`) against `None` or an `ENUM.MEMBER`, the
   expensive tests are held back and the cheap ones are run. The held tests
   come back if any cheap test fails or does not run.
7. **Exact check for `test_optimized_tpch`.** Its only sqlglot step is
   transpiling 22 TPC-H queries, and their optimized forms, to DuckDB. If
   that SQL text is unchanged, so is the result.

8. **Changed fixtures are served patched.** Text fixtures (`.sql`, `.json`)
   and sources read as text are served from memory to `open()` and
   `Path.read_text` in the children, so fixture commits are verified like code.
   A change to `tpc-ds.sql` or `tpc-h.sql` (read by all 61 `TestExecutor`
   tests, about 33 s) is checked by fingerprinting every query with the new
   text and replaying only the changed ones.
9. **Fixture entries.** Tests that loop over fixture files (`check_file` in
   `test_optimizer`, identity loops) have per-entry coverage in
   `map/fxunits.json`. When a test was selected only through changed lines,
   the child runs just the entries that executed them.
10. **Two verification rounds.** The second round runs after the replica and
    TPC checks have dropped a big test, when running the cheap rest starts to
    pay. Commits that break an import are still checked, except for the
    tests that import the broken module.

`PYTHONHASHSEED=0` keeps the TPC fingerprints deterministic.

Any error inside these checks falls back to the static selection.
A deadline (110 s) and a budget keep the batch well inside the 3-minute limit.

The chosen tests are split with longest-processing-time-first for every
worker count from 1 to 8. The cheapest result counts: busiest worker plus
2 s per worker.

## Layout

```
Dockerfile          python:3.13-slim (the labelling image), sqlglot tarball at eb11aec, selector + map
selector.py         the selector (stdlib only)
runner.py           run-time checks in forked children (import check, tests, TPC query replay)
map/                committed coverage map (~5 MB)
tools/
  mapplugin.py      pytest plugin used only while building the map
  make_map.sh       full map rebuild: suite under coverage + import trace + static facts
  build_map.py      merges per-worker coverage into map/
  importtrace.py    import-time trace
  build_static.py   names, import graph, class-attribute copies, import-time effects
  run_local.sh      run the selector natively and score it with kit/score.py
  mutate.py         synthetic commits for validation
  truth.py          ground truth for those commits (runs the real suite per patch)
kit/                the starter kit's sample commits, score.py and requirements.lock
```

## Running locally

```sh
make setup      # once: pinned sqlglot checkout + Python 3.13 venv (needs git and uv)
make local      # selector on the 30 sample commits, scored, no Docker needed
make map        # rebuild map/ after changing the tools (~1 minute)

# synthetic validation
.work/venv/bin/python tools/mutate.py .work/sqlglot map .work/val 80 7
cp kit/sample/tests.json .work/val/
.work/venv/bin/python tools/truth.py .work/sqlglot .work/val 8   # ~25 s per commit
make validate

make sample     # with Docker: build and run under the scorer's limits
```

The map does not depend on any patch. It is built only from the pinned
commit (coverage, imports, fixtures), as the rules allow.
