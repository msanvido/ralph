"""LongBench (narrativeqa) benchmark for Ralph.

Ported from fast-rlm/benchmarks/longbench_benchmark.py. The fast-rlm version
runs a single example from THUDM/LongBench narrativeqa and prints the
expected answer alongside the agent's. We do the same shape here, but adapted
to Ralph's "iterate-on-a-workspace" model:

  1. Pull one (or N) example from the dataset.
  2. For each example: spin up benchmark/_runs/longbench_idx<N>/, write a
     prompt.md asking Ralph to read the context from `context.txt` and write
     its final answer into `solution.txt`.
  3. Run `python -m ralph` against that workspace (timeout 600s).
  4. Read solution.txt and print it next to the dataset's reference answers
     for human grading (these answers are free-form text — no auto-grader).

Run:  python benchmark/longbench.py
      python benchmark/longbench.py --idx 140
      python benchmark/longbench.py --idx 100 --idx 140
      python benchmark/longbench.py --task narrativeqa --idx 140 --model anthropic/claude-sonnet-4-6

Install:  pip install -e '.[benchmarks]'   (adds the `datasets` package).
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
RUNS_DIR = HERE / "_runs"

PROMPT_TEMPLATE = """\
Answer the question below using the context in `context.txt` (already in this
workspace).

# Question
{question}

# How to work
- The context is too long to keep in your head; use `read` with line offsets
  and `grep` to navigate. Don't try to read the whole file in one shot.
- Search for keywords from the question first to locate relevant passages.
- Keep your answer concise (one sentence to a short paragraph).
- Write the final answer to `solution.txt` (plain text, no markdown).

mark_done once `solution.txt` is written.
"""


def preflight() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ralph", "--help"],
        capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        msg = (proc.stderr + proc.stdout).strip()[-1000:]
        sys.exit(
            f"`{sys.executable} -m ralph --help` failed (rc={proc.returncode}).\n"
            f"Try: .venv/bin/python benchmark/longbench.py {' '.join(sys.argv[1:])}\n\n"
            f"Subprocess output:\n{msg}"
        )
    try:
        import datasets  # noqa: F401
    except ImportError:
        sys.exit(
            "The `datasets` package is required for this benchmark.\n"
            "Install with: pip install -e '.[benchmarks]'"
        )


def run_ralph(workspace: Path, prompt: Path, model: str | None, timeout: int) -> tuple[int, float, str]:
    cmd = [sys.executable, "-m", "ralph",
           "--workspace", str(workspace),
           "--prompt", str(prompt),
           "--bus-dir", str(workspace / "_bus")]
    if model:
        cmd += ["--model", model]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    tail = (proc.stdout + proc.stderr).strip()[-1500:]
    return proc.returncode, time.time() - started, tail


def run_one(example: dict, idx: int, model: str | None, timeout: int) -> dict:
    name = f"longbench_idx{idx}"
    ws = RUNS_DIR / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)

    (ws / "context.txt").write_text(example["context"])
    prompt_path = ws / "prompt.md"
    prompt_path.write_text(PROMPT_TEMPLATE.format(question=example["input"]))

    print(f"  ▶ running ralph (context: {len(example['context']):,} chars)...", flush=True)
    try:
        rc, elapsed, tail = run_ralph(ws, prompt_path, model, timeout)
    except subprocess.TimeoutExpired:
        return {"idx": idx, "elapsed": float(timeout), "answer": "", "reason": f"timed out ({timeout}s)"}

    answer_path = ws / "solution.txt"
    answer = answer_path.read_text().strip() if answer_path.exists() else ""
    reason = "" if answer else f"no solution.txt (rc={rc}); tail:\n{tail}"
    return {"idx": idx, "elapsed": elapsed, "answer": answer, "reason": reason,
            "expected": example["answers"]}


def main():
    p = argparse.ArgumentParser(description="Run a LongBench example against Ralph.")
    p.add_argument("--task", type=str, default="narrativeqa",
                   help="LongBench subtask (default: narrativeqa)")
    p.add_argument("--idx", type=int, action="append",
                   help="Dataset index(es) to run. Repeat for multiple. Default: 140")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task subprocess timeout in seconds (default 600)")
    args = p.parse_args()
    indices = args.idx or [140]

    preflight()
    from datasets import load_dataset
    print(f"Loading THUDM/LongBench [{args.task}] split=test ...", flush=True)
    ds = load_dataset("THUDM/LongBench", args.task, split="test", trust_remote_code=True)

    RUNS_DIR.mkdir(exist_ok=True)
    results = []
    for i in indices:
        if i >= len(ds):
            print(f"⚠️  idx {i} out of range (dataset has {len(ds)} rows); skipping")
            continue
        ex = ds[i]
        print(f"\n{'─' * 50}\n▶ longbench/{args.task} idx={i}")
        print(f"  Question: {ex['input'][:200]}")
        r = run_one(ex, i, args.model, args.timeout)
        results.append(r)
        print(f"  ⏱ {r['elapsed']:.1f}s")
        print(f"  📝 ralph:    {r['answer'] or '(empty)'}")
        print(f"  ✅ expected: {r['expected']}")
        if r.get("reason"):
            print(f"  reason: {r['reason']}")

    print(f"\n{'=' * 50}\nRan {len(results)} task(s). Answers are free-form text — grade by hand.")


if __name__ == "__main__":
    main()
