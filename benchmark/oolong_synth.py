"""OolongBench (oolong-synth) benchmark for Ralph.

Ported from fast-rlm/benchmarks/oolong_synth_benchmark.py. The fast-rlm
version runs a single example (idx=100) from oolongbench/oolong-synth and
prints the expected answer next to the agent's. We do the same here but
adapt to Ralph's workspace model — context goes into a file, Ralph answers
into solution.txt.

The dataset has three task groups: 'timeline', 'user', 'counting'. Use
--task-group to restrict, or --idx to pick a specific row.

Run:  python benchmark/oolong_synth.py
      python benchmark/oolong_synth.py --idx 100
      python benchmark/oolong_synth.py --task-group counting --idx 5
      python benchmark/oolong_synth.py --model anthropic/claude-sonnet-4-6

Install:  pip install -e '.[benchmarks]'
"""
import argparse
import shutil
import subprocess

from _common import RUNS_DIR, preflight, run_ralph

PROMPT_TEMPLATE = """\
Answer the question below using the labeled context window in `context.txt`
(already in this workspace).

# Question
{question}

# How to work
- `context.txt` contains the labeled context window. Read it with offsets
  and use grep to find the labels / markers the question refers to.
- Be precise — these answers are exact (a timeline ordering, a user id, a
  count). Don't paraphrase.
- Write only the final answer to `solution.txt` (plain text, no markdown,
  no explanation).

mark_done once `solution.txt` is written.
"""


def run_one(example: dict, idx: int, model: str | None, timeout: int) -> dict:
    name = f"oolong_synth_idx{idx}"
    ws = RUNS_DIR / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)

    (ws / "context.txt").write_text(example["context_window_text_with_labels"])
    prompt_path = ws / "prompt.md"
    prompt_path.write_text(PROMPT_TEMPLATE.format(question=example["question"]))

    ctx_len = len(example["context_window_text_with_labels"])
    print(f"  ▶ running ralph (context: {ctx_len:,} chars)...", flush=True)
    try:
        rc, elapsed, tail = run_ralph(ws, prompt_path, model, timeout=timeout,
                                      extra_args=["--exit-on-done"])
    except subprocess.TimeoutExpired:
        return {"idx": idx, "elapsed": float(timeout), "answer": "", "reason": f"timed out ({timeout}s)"}

    answer_path = ws / "solution.txt"
    answer = answer_path.read_text().strip() if answer_path.exists() else ""
    reason = "" if answer else f"no solution.txt (rc={rc}); tail:\n{tail}"
    return {"idx": idx, "elapsed": elapsed, "answer": answer, "reason": reason,
            "expected": example["answer"],
            "task_group": example.get("task_group", "")}


def main():
    p = argparse.ArgumentParser(description="Run an oolong-synth example against Ralph.")
    p.add_argument("--idx", type=int, action="append",
                   help="Dataset index(es) (after task-group filter). Repeat for multiple. Default: 100")
    p.add_argument("--task-group", type=str, default=None,
                   choices=["timeline", "user", "counting"],
                   help="Restrict to one task group (default: all)")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task subprocess timeout in seconds (default 600)")
    args = p.parse_args()
    indices = args.idx or [100]

    preflight("oolong_synth.py", needs_datasets=True)
    from datasets import load_dataset
    print("Loading oolongbench/oolong-synth split=test ...", flush=True)
    ds = load_dataset("oolongbench/oolong-synth", split="test")
    if args.task_group:
        ds = ds.filter(lambda x: x["task_group"] == args.task_group)
        print(f"  filtered to task_group={args.task_group}: {len(ds)} rows")

    RUNS_DIR.mkdir(exist_ok=True)
    results = []
    for i in indices:
        if i >= len(ds):
            print(f"⚠️  idx {i} out of range (dataset has {len(ds)} rows); skipping")
            continue
        ex = ds[i]
        print(f"\n{'─' * 50}\n▶ oolong_synth idx={i}  task_group={ex.get('task_group', '?')}")
        print(f"  Question: {ex['question'][:200]}")
        r = run_one(ex, i, args.model, args.timeout)
        results.append(r)
        print(f"  ⏱ {r['elapsed']:.1f}s")
        print(f"  📝 ralph:    {r['answer'] or '(empty)'}")
        print(f"  ✅ expected: {r['expected']}")
        if r.get("reason"):
            print(f"  reason: {r['reason']}")

    if results:
        # The oolong answers are often exact strings — try a naive exact-match grade.
        exact = sum(1 for r in results if str(r["answer"]).strip() == str(r["expected"]).strip())
        print(f"\n{'=' * 50}\nRan {len(results)} task(s). Exact-string match: {exact}/{len(results)}")
        print("(Match is a hint, not authoritative — many oolong answers admit equivalent phrasings.)")


if __name__ == "__main__":
    main()
