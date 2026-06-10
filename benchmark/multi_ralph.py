"""Multi-Ralph benchmark: what does the bus buy a novice, quantitatively?

The README's sudoku expert/novice demo, made measurable. Two conditions:

  bus   — an expert Ralph builds a sudoku solver and idles on the bus; then a
          novice Ralph (--wait-for-peer expert) gets a fresh puzzle. The peer
          preamble tells it the expert exists; fetching the solver via
          ask_ralph beats rebuilding it.
  solo  — the same novice prompt, same puzzle, no peers on the bus. The novice
          must build its own solver.

Both runs are timed and the solution is machine-verified (clues preserved,
rows/columns/boxes valid). The report also says whether the bus novice actually
installed a tool from the expert, so a fast-but-self-built run can't masquerade
as sharing.

Run:  python benchmark/multi_ralph.py                 # both conditions
      python benchmark/multi_ralph.py --condition bus
      python benchmark/multi_ralph.py --model anthropic/claude-sonnet-4-6
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from _common import RUNS_DIR, list_tools, preflight, run_ralph

PUZZLE = "500080049000500030067300001150000000000208000000000018700004150030002000490050003"

EXPERT_PROMPT = """Build a sudoku solver in tools/sudoku_solver.py. It must take an 81-char
puzzle string (digits, 0 for blanks) and return the solved 81-char string.
Test it on at least one puzzle. mark_done when the solver works.
"""

NOVICE_PROMPT = f"""Solve this sudoku puzzle: {PUZZLE}

(0 = blank cell.) Write the solved 81-character digit string — nothing else —
to solution.txt, then mark_done.
"""


def verify_sudoku(puzzle: str, answer: str) -> tuple[bool, str]:
    answer = answer.strip()
    if len(answer) != 81 or not answer.isdigit() or "0" in answer:
        return False, f"not an 81-digit solution: {answer[:90]!r}"
    for i, clue in enumerate(puzzle):
        if clue != "0" and answer[i] != clue:
            return False, f"clue at cell {i} changed ({clue} -> {answer[i]})"
    rows = [answer[r * 9:(r + 1) * 9] for r in range(9)]
    cols = ["".join(answer[r * 9 + c] for r in range(9)) for c in range(9)]
    boxes = ["".join(answer[(br * 3 + r) * 9 + bc * 3 + c]
                     for r in range(3) for c in range(3))
             for br in range(3) for bc in range(3)]
    for group in rows + cols + boxes:
        if set(group) != set("123456789"):
            return False, f"invalid group: {group}"
    return True, ""


def expert_is_done(bus_dir: Path) -> bool:
    """Parse the expert's status file (written atomically by the bus) for done=true."""
    status = bus_dir / "expert.status"
    if not status.exists():
        return False
    try:
        return json.loads(status.read_text()).get("done") is True
    except (json.JSONDecodeError, OSError):
        return False


def run_condition(condition: str, model: str | None, timeout: int) -> dict:
    base = RUNS_DIR / f"multi_ralph_{condition}"
    if base.exists():
        shutil.rmtree(base)
    bus_dir = base / "bus"
    novice_ws = base / "ws_novice"
    novice_ws.mkdir(parents=True)
    novice_prompt = novice_ws / "prompt.md"
    novice_prompt.write_text(NOVICE_PROMPT)

    expert_proc = None
    expert_tools: set[str] = set()
    try:
        if condition == "bus":
            expert_ws = base / "ws_expert"
            expert_ws.mkdir(parents=True)
            expert_prompt = expert_ws / "prompt.md"
            expert_prompt.write_text(EXPERT_PROMPT)
            cmd = [sys.executable, "-m", "ralph",
                   "--workspace", str(expert_ws),
                   "--prompt", str(expert_prompt),
                   "--bus-dir", str(bus_dir),
                   "--ralph-id", "expert"]
            if model:
                cmd += ["--model", model]
            # No --exit-on-done: the expert must IDLE on the bus after mark_done
            # so it can serve the novice's ask_ralph.
            print("  ▶ starting expert (builds the solver, then serves the bus)...", flush=True)
            expert_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)

        # The novice itself waits for the expert via --wait-for-peer, so its
        # elapsed time would include the expert's build time. Time only the
        # novice's own work by waiting for the expert here first.
        extra = ["--exit-on-done", "--ralph-id", "novice"]
        if condition == "bus":
            deadline = time.time() + timeout
            while time.time() < deadline:
                if expert_is_done(bus_dir):
                    break
                if expert_proc.poll() is not None:
                    return {"condition": condition, "passed": False, "elapsed": 0.0,
                            "reason": f"expert exited early (rc={expert_proc.returncode})"}
                time.sleep(1.0)
            else:
                return {"condition": condition, "passed": False, "elapsed": float(timeout),
                        "reason": "expert never finished building the solver"}
            expert_tools = list_tools(base / "ws_expert")
            print(f"  ✓ expert done (tools: {sorted(expert_tools)})", flush=True)

        print("  ▶ running novice...", flush=True)
        try:
            rc, elapsed, tail = run_ralph(novice_ws, novice_prompt, model,
                                          timeout=timeout, extra_args=extra,
                                          bus_dir=bus_dir)
        except subprocess.TimeoutExpired:
            return {"condition": condition, "passed": False, "elapsed": float(timeout),
                    "reason": "novice timed out"}

        answer_path = novice_ws / "solution.txt"
        answer = answer_path.read_text() if answer_path.exists() else ""
        passed, reason = verify_sudoku(PUZZLE, answer)
        if not passed and tail:
            reason += f"\n  novice rc={rc}; tail:\n{tail[-400:]}"
        fetched = sorted(list_tools(novice_ws) & expert_tools) if condition == "bus" else []
        return {"condition": condition, "passed": passed, "elapsed": elapsed,
                "reason": reason, "fetched_from_expert": fetched}
    finally:
        if expert_proc is not None and expert_proc.poll() is None:
            expert_proc.terminate()
            try:
                expert_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                expert_proc.kill()


def main():
    p = argparse.ArgumentParser(description="Time a novice Ralph with vs without a peer expert.")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--condition", choices=["bus", "solo"], default=None,
                   help="Run only one condition (default: both)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-phase timeout in seconds (default 600)")
    args = p.parse_args()

    preflight("multi_ralph.py")
    RUNS_DIR.mkdir(exist_ok=True)

    conditions = [args.condition] if args.condition else ["bus", "solo"]
    results = []
    for condition in conditions:
        print(f"\n{'─' * 50}\n▶ condition: {condition}")
        r = run_condition(condition, args.model, args.timeout)
        results.append(r)
        symbol = "✅" if r["passed"] else "❌"
        print(f"{symbol} {condition}: {r['elapsed']:.1f}s")
        if condition == "bus" and r["passed"]:
            fetched = r.get("fetched_from_expert") or []
            if fetched:
                print(f"  📦 novice installed from expert: {fetched}")
            else:
                print("  ⚠️  novice solved it WITHOUT fetching the expert's tool "
                      "(fast, maybe, but it isn't sharing)")
        if not r["passed"]:
            print(f"  reason: {r['reason']}")

    if len(results) == 2 and all(r["passed"] for r in results):
        bus_t = next(r["elapsed"] for r in results if r["condition"] == "bus")
        solo_t = next(r["elapsed"] for r in results if r["condition"] == "solo")
        print(f"\n{'=' * 50}")
        print(f"Novice with expert on the bus: {bus_t:.1f}s")
        print(f"Novice alone:                  {solo_t:.1f}s")
        print(f"Sharing saved:                 {solo_t - bus_t:+.1f}s "
              f"({(1 - bus_t / solo_t) * 100:+.0f}%)" if solo_t else "")
        print("(Single puzzle, single run — repeat a few times before quoting numbers.)")


if __name__ == "__main__":
    main()
