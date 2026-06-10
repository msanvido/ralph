# Benchmarks

End-to-end evaluations of Ralph. Five are bundled:

- `run.py` — HumanEval subset (code generation, auto-graded). Details below.
- `tool_reuse.py` — five related text-analysis tasks against ONE shared workspace;
  measures whether tools built for task 1 actually get called on tasks 2–5
  (detected via LRU timestamps). `--fresh` runs the no-carryover control.
- `multi_ralph.py` — the sudoku expert/novice demo made quantitative: novice
  time-to-solution with the expert on the bus vs alone, plus whether the novice
  really installed the expert's tool (machine-verified solution).
- `longbench.py` — single example from THUDM/LongBench narrativeqa (long-context QA, manual grade).
- `oolong_synth.py` — single example from oolongbench/oolong-synth (long-context structured QA, exact-string hint).

The first three measure what makes Ralph *different* — tool creation, tool
reuse across tasks, and peer sharing — not just raw code generation.

All runners launch Ralph with `--exit-on-done`, so the subprocess exit code
distinguishes "called mark_done" (0) from "ran out of iterations" (2); shared
plumbing lives in `_common.py`.

The two long-context benchmarks need the `datasets` extra:

```sh
pip install -e '.[benchmarks]'

python benchmark/longbench.py                           # idx=140 by default
python benchmark/longbench.py --idx 100 --idx 140
python benchmark/oolong_synth.py                        # idx=100 default
python benchmark/oolong_synth.py --task-group counting --idx 5
```

Per-example workspaces are created under `benchmark/_runs/longbench_idx<N>/`
and `benchmark/_runs/oolong_synth_idx<N>/`. Ralph reads the context from
`context.txt` and writes its answer to `solution.txt`. Grading is largely
manual — answers are free-form text — so the runner just prints Ralph's
answer next to the dataset reference. oolong-synth answers are often exact,
so the runner also reports an exact-string match count as a rough hint.

Ported from [fast-rlm](https://github.com/avbiswas/fast-rlm)'s benchmarks of
the same name.

---

## HumanEval subset

A small subset of [HumanEval](https://github.com/openai/human-eval) (16 tasks
spanning the difficulty range) to measure Ralph end-to-end: prompt-to-passing-tests.

## Run

```sh
python benchmark/run.py                              # all 16 tasks, default model
python benchmark/run.py --task HumanEval/0           # one task
python benchmark/run.py --model anthropic/claude-sonnet-4-6
```

Each task gets its own workspace under `benchmark/_runs/humaneval_X/`. The
runner:
1. Writes `prompt.md` (the function signature + docstring) and
   `test_solution.py` (the canonical HumanEval `check`) into the workspace.
2. Runs `python -m ralph` as a subprocess against that workspace (timeout: 600s).
3. After Ralph marks done, runs `python test_solution.py` and grades on the
   exit code.

Per-task workspaces are persistent — re-running re-creates them from scratch,
so you can `cat benchmark/_runs/humaneval_7/solution.py` to inspect what Ralph
produced.

## What this measures

End-to-end: function comprehension + Python implementation + self-checking
discipline (since Ralph has no `bash`/test-runner tool — solutions are
correct-by-construction or they fail). It's a much harder bar than direct
function-completion scoring (HumanEval pass@1 typically reports the model's
ability to fill in a function given a prompt, with no agent loop). Ralph has
to manage its own workspace, decide when it's done, and not regress.

Results are graded on TWO axes, reported separately:

- **code correct** — the canonical HumanEval `check` passes against `solution.py`
  (verified even when Ralph timed out or never called mark_done).
- **mark_done called** — Ralph itself decided the task was complete.

A ✅ needs both. A 🟡 means the code was right but Ralph never declared done —
the meta-protocol gap (when to stop) rather than a toolmaking failure. Early
runs suggest this gap is a real, distinct failure mode worth tracking.

## Tool reuse and multi-Ralph

```sh
python benchmark/tool_reuse.py            # shared workspace — reuse enabled
python benchmark/tool_reuse.py --fresh    # control — fresh workspace per task
python benchmark/multi_ralph.py           # bus + solo conditions, timed
```

`tool_reuse.py` reports, per task, which tools were created and which
*pre-existing* tools were called (their `tools/_lru.py` timestamp advanced).
Compare total time and pass rate against `--fresh` to see what persistence buys.

`multi_ralph.py` prints novice time-to-solution with and without the expert,
and flags the case where the novice solved the puzzle without fetching the
expert's tool (fast, but not sharing). Single puzzle, single run — repeat
before quoting numbers.

## Tasks

Hand-picked across difficulty: `HumanEval/0, 1, 7, 14, 23, 34, 47, 51, 65, 75,
89, 102, 121, 132, 148, 162`. The full bundle lives in
`humaneval_subset.jsonl` — extend or trim as you like.

To pull a different subset from the upstream dataset, see the inline scripts
in this README's git history (or download `HumanEval.jsonl.gz` from
github.com/openai/human-eval and filter to the IDs you want).
