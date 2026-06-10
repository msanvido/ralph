"""HumanEval-subset benchmark for Ralph.

For each task in humaneval_subset.jsonl:
  1. Spin up a fresh workspace under benchmark/_runs/<task_id>.
  2. Write a prompt.md asking Ralph to complete the function in solution.py.
  3. Run `python -m ralph --exit-on-done` as a subprocess against that workspace.
  4. Import solution.py and run the canonical HumanEval `check(candidate)` against
     it — even when Ralph timed out or never called mark_done, because "the code is
     right but the contract wasn't completed" is a result we track separately.
  5. Report per-task: code correctness AND mark_done discipline.

Run:  python benchmark/run.py
      python benchmark/run.py --task HumanEval/0
      python benchmark/run.py --model anthropic/claude-sonnet-4-6
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _common import RUNS_DIR, preflight, run_ralph

HERE = Path(__file__).parent
TASKS_FILE = HERE / "humaneval_subset.jsonl"

PROMPT_TEMPLATE = """Implement the function in `solution.py`. The signature and docstring are below;
keep them verbatim and write the function body. Do NOT modify the tests in `test_solution.py`.

```python
{prompt}
```

When `solution.py` exists with `{entry_point}` defined and you believe it satisfies the docstring,
call mark_done.
"""

TEST_TEMPLATE = """\
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from solution import {entry_point} as candidate

{test}

if __name__ == "__main__":
    check(candidate)
    print("PASS")
"""


def load_tasks() -> list[dict]:
    return [json.loads(line) for line in TASKS_FILE.read_text().splitlines() if line.strip()]


def slug(task_id: str) -> str:
    return task_id.replace("/", "_").lower()


def verify(workspace: Path) -> tuple[bool, str]:
    """Import the candidate's solution and run the bundled `check` against it."""
    workspace = workspace.resolve()
    if not (workspace / "solution.py").exists():
        return False, "solution.py not found"
    proc = subprocess.run(
        [sys.executable, str(workspace / "test_solution.py")],
        capture_output=True, text=True, cwd=workspace, timeout=30,
    )
    if proc.returncode == 0 and "PASS" in proc.stdout:
        return True, ""
    return False, (proc.stderr or proc.stdout)[-500:]


def run_task(task: dict, model: str | None, timeout: int) -> dict:
    name = slug(task["task_id"])
    ws = RUNS_DIR / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)

    # Seed: prompt + canonical tests.
    prompt_path = ws / "prompt.md"
    prompt_path.write_text(PROMPT_TEMPLATE.format(**task))
    (ws / "test_solution.py").write_text(TEST_TEMPLATE.format(**task))

    print(f"  ▶ running ralph...", flush=True)
    try:
        rc, elapsed, tail = run_ralph(ws, prompt_path, model, timeout=timeout,
                                      extra_args=["--exit-on-done"])
        timed_out = False
    except subprocess.TimeoutExpired:
        rc, elapsed, tail = -1, float(timeout), ""
        timed_out = True

    # rc 0 = mark_done was called; rc 2 = max iterations without it; timeout = it
    # never decided. Verify the code in ALL cases — a correct solution without a
    # mark_done is the meta-protocol failure we want to count, not hide.
    marked_done = rc == 0
    code_passed, reason = verify(ws)
    if not code_passed and not marked_done and tail:
        reason = f"{reason}\nralph rc={rc}{' (timeout)' if timed_out else ''}; tail:\n{tail}"

    return {
        "task": task["task_id"],
        "passed": code_passed and marked_done,
        "code_passed": code_passed,
        "marked_done": marked_done,
        "timed_out": timed_out,
        "elapsed": elapsed,
        "reason": "" if code_passed and marked_done else reason,
    }


def main():
    p = argparse.ArgumentParser(description="Run HumanEval subset against Ralph.")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--task", type=str, default=None, help="Run only this task id (e.g. HumanEval/0)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task subprocess timeout in seconds (default 600)")
    args = p.parse_args()

    preflight("run.py")

    tasks = load_tasks()
    if args.task:
        tasks = [t for t in tasks if t["task_id"] == args.task]
        if not tasks:
            sys.exit(f"task not found: {args.task}")

    print(f"Running {len(tasks)} task(s)" + (f" with model={args.model}" if args.model else ""))
    RUNS_DIR.mkdir(exist_ok=True)
    results = []
    for task in tasks:
        print(f"\n{'─' * 50}\n▶ {task['task_id']} ({task['entry_point']})")
        r = run_task(task, args.model, args.timeout)
        results.append(r)
        symbol = "✅" if r["passed"] else ("🟡" if r["code_passed"] else "❌")
        print(f"{symbol} {r['task']} ({r['elapsed']:.1f}s)")
        if not r["passed"]:
            print(f"  reason: {r['reason'] or 'code correct but mark_done never called'}")

    full = sum(r["passed"] for r in results)
    code_only = sum(r["code_passed"] and not r["marked_done"] for r in results)
    print(f"\n{'=' * 50}")
    print(f"Summary: {full}/{len(results)} passed (code correct + mark_done called)")
    if code_only:
        print(f"         {code_only} more had CORRECT code but never called mark_done")
        print(f"         (meta-protocol gap: the toolmaking worked; the done-discipline didn't)")
    print(f"{'=' * 50}")
    for r in results:
        symbol = "✅" if r["passed"] else ("🟡" if r["code_passed"] else "❌")
        flags = []
        if r["code_passed"] and not r["marked_done"]:
            flags.append("code OK, no mark_done")
        if r["timed_out"]:
            flags.append("timeout")
        first_line = r["reason"].splitlines()[0] if r["reason"] and not flags else "; ".join(flags)
        print(f"  {symbol} {r['task']:<20} {r['elapsed']:>6.1f}s  {first_line[:60]}")


if __name__ == "__main__":
    main()
