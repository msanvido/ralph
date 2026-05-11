"""HumanEval-subset benchmark for Ralph.

For each task in humaneval_subset.jsonl:
  1. Spin up a fresh workspace under benchmark/_runs/<task_id>.
  2. Write a prompt.md asking Ralph to complete the function in solution.py.
  3. Run `python -m ralph` as a subprocess against that workspace.
  4. After Ralph marks done (or hits MAX_ITERATIONS), import solution.py and
     run the canonical HumanEval `check(candidate)` against it.
  5. Report pass/fail + elapsed time.

Run:  python benchmark/run.py
      python benchmark/run.py --task HumanEval/0
      python benchmark/run.py --model anthropic/claude-sonnet-4-6
"""
import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
TASKS_FILE = HERE / "humaneval_subset.jsonl"
RUNS_DIR = HERE / "_runs"

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


def run_ralph(workspace: Path, prompt: Path, model: str | None) -> tuple[int, float]:
    cmd = [sys.executable, "-m", "ralph",
           "--workspace", str(workspace),
           "--prompt", str(prompt),
           "--bus-dir", str(workspace / "_bus")]
    if model:
        cmd += ["--model", model]
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return proc.returncode, time.time() - started


def verify(workspace: Path, entry_point: str) -> tuple[bool, str]:
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


def run_task(task: dict, model: str | None) -> dict:
    name = slug(task["task_id"])
    ws = RUNS_DIR / name
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)

    # Seed: prompt + canonical tests.
    prompt_path = ws / "prompt.md"
    prompt_path.write_text(PROMPT_TEMPLATE.format(**task))
    (ws / "test_solution.py").write_text(TEST_TEMPLATE.format(**task))

    # Run Ralph. Daemonize the bus so it doesn't pollute the parent dir.
    print(f"  ▶ running ralph...", flush=True)
    try:
        rc, elapsed = run_ralph(ws, prompt_path, model)
    except subprocess.TimeoutExpired:
        return {"task": task["task_id"], "passed": False, "elapsed": 600.0,
                "reason": "ralph timed out (600s)"}

    # Verify against the bundled HumanEval test.
    passed, reason = verify(ws, task["entry_point"])
    return {
        "task": task["task_id"], "passed": passed, "elapsed": elapsed,
        "reason": reason if not passed else "", "ralph_rc": rc,
    }


def main():
    p = argparse.ArgumentParser(description="Run HumanEval subset against Ralph.")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--task", type=str, default=None, help="Run only this task id (e.g. HumanEval/0)")
    args = p.parse_args()

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
        r = run_task(task, args.model)
        results.append(r)
        symbol = "✅" if r["passed"] else "❌"
        print(f"{symbol} {r['task']} ({r['elapsed']:.1f}s)")
        if not r["passed"]:
            print(f"  reason: {r['reason']}")

    print(f"\n{'=' * 50}\nSummary: {sum(r['passed'] for r in results)}/{len(results)} passed")
    print(f"{'=' * 50}")
    for r in results:
        symbol = "✅" if r["passed"] else "❌"
        print(f"  {symbol} {r['task']:<20} {r['elapsed']:>6.1f}s  {r['reason'][:60]}")


if __name__ == "__main__":
    main()
