# Benchmarks

End-to-end evaluations of Ralph on public datasets. Three are bundled:

- `run.py` — HumanEval subset (code generation, auto-graded). Details below.
- `longbench.py` — single example from THUDM/LongBench narrativeqa (long-context QA, manual grade).
- `oolong_synth.py` — single example from oolongbench/oolong-synth (long-context structured QA, exact-string hint).

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

## Tasks

Hand-picked across difficulty: `HumanEval/0, 1, 7, 14, 23, 34, 47, 51, 65, 75,
89, 102, 121, 132, 148, 162`. The full bundle lives in
`humaneval_subset.jsonl` — extend or trim as you like.

To pull a different subset from the upstream dataset, see the inline scripts
in this README's git history (or download `HumanEval.jsonl.gz` from
github.com/openai/human-eval and filter to the IDs you want).
