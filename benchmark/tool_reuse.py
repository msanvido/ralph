"""Tool-reuse benchmark: does Ralph's toolbelt carry across related tasks?

This measures the thing the other benchmarks don't: whether tools created for
one task get REUSED on later, related tasks. Five string/text-analysis tasks
run sequentially against the SAME workspace (so workspace/tools/ persists).
Before each task we snapshot tools/_lru.py; after, any pre-existing tool whose
LRU timestamp advanced was actually *called* during the task — that's a reuse.

Run:  python benchmark/tool_reuse.py                  # shared workspace (reuse enabled)
      python benchmark/tool_reuse.py --fresh          # control: fresh workspace per task
      python benchmark/tool_reuse.py --model anthropic/claude-sonnet-4-6

Compare the two modes' total elapsed time and pass rates: the delta is what
tool persistence buys.
"""
import argparse
import shutil
import subprocess
from pathlib import Path

from _common import RUNS_DIR, list_tools, preflight, run_ralph
from ralph.tools import read_py_literal

WORDS = [
    "level", "banana", "racecar", "apple", "noon", "kayak", "grape", "civic",
    "rotor", "melon", "radar", "cherry", "refer", "papaya", "stats", "mango",
    "tenet", "lemon", "madam", "peach", "deed", "plum", "wow", "berry",
    "solos", "kiwi", "minim", "fig", "sagas", "date", "coconut", "apricot",
    "avocado", "blueberry", "cranberry", "currant", "dragonfruit", "elderberry",
    "guava", "jackfruit", "lychee", "nectarine", "orange", "pineapple",
    "pomegranate", "raspberry", "strawberry", "tangerine", "watermelon", "honeydew",
]

VOWELS = set("aeiou")


def _expected_vowel_counts() -> dict[str, int]:
    return {w: sum(c in VOWELS for c in w) for w in WORDS}


def _expected_palindromes() -> set[str]:
    return {w for w in WORDS if w == w[::-1]}


def _expected_double_letters() -> set[str]:
    return {w for w in WORDS if any(a == b for a, b in zip(w, w[1:]))}


def _expected_letter_freq() -> dict[str, int]:
    freq: dict[str, int] = {}
    for w in WORDS:
        for c in w:
            freq[c] = freq.get(c, 0) + 1
    return freq


def _expected_longest() -> str:
    return max(WORDS, key=len)


def _parse_colon_lines(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _verify_vowels(ws: Path) -> tuple[bool, str]:
    f = ws / "vowel_counts.txt"
    if not f.exists():
        return False, "vowel_counts.txt missing"
    got = _parse_colon_lines(f.read_text())
    expected = _expected_vowel_counts()
    bad = [w for w, n in expected.items() if got.get(w) != str(n)]
    return (not bad), f"wrong/missing counts for: {bad[:5]}" if bad else ""


def _verify_palindromes(ws: Path) -> tuple[bool, str]:
    f = ws / "palindromes.txt"
    if not f.exists():
        return False, "palindromes.txt missing"
    got = {line.strip().lower() for line in f.read_text().splitlines() if line.strip()}
    expected = _expected_palindromes()
    return (got == expected), f"got {sorted(got)} expected {sorted(expected)}" if got != expected else ""


def _verify_doubles(ws: Path) -> tuple[bool, str]:
    f = ws / "double_letters.txt"
    if not f.exists():
        return False, "double_letters.txt missing"
    got = {line.strip().lower() for line in f.read_text().splitlines() if line.strip()}
    expected = _expected_double_letters()
    return (got == expected), f"diff: extra={sorted(got - expected)} missing={sorted(expected - got)}" if got != expected else ""


def _verify_freq(ws: Path) -> tuple[bool, str]:
    f = ws / "letter_freq.txt"
    if not f.exists():
        return False, "letter_freq.txt missing"
    got = _parse_colon_lines(f.read_text())
    expected = _expected_letter_freq()
    bad = [c for c, n in expected.items() if got.get(c) != str(n)]
    return (not bad), f"wrong/missing freq for: {bad[:5]}" if bad else ""


def _verify_longest(ws: Path) -> tuple[bool, str]:
    f = ws / "longest.txt"
    if not f.exists():
        return False, "longest.txt missing"
    got = f.read_text().strip().lower()
    expected = _expected_longest()
    return (got == expected), f"got {got!r} expected {expected!r}" if got != expected else ""


TASKS = [
    ("vowel_counts",
     "For every word in words.txt, count its vowels (a, e, i, o, u). Write "
     "vowel_counts.txt with one `word: count` line per word (lowercase word, "
     "colon, space, integer), in the same order as words.txt.",
     _verify_vowels),
    ("palindromes",
     "Find every palindrome in words.txt. Write palindromes.txt with one "
     "lowercase word per line (any order). A palindrome reads the same "
     "forwards and backwards.",
     _verify_palindromes),
    ("double_letters",
     "Find every word in words.txt containing the same letter twice in a row "
     "(like the 'rr' in 'berry'). Write double_letters.txt, one lowercase word "
     "per line, any order.",
     _verify_doubles),
    ("letter_freq",
     "Count how many times each letter a-z appears across ALL words in "
     "words.txt combined. Write letter_freq.txt with one `letter: count` line "
     "per letter that appears at least once (lowercase letter, colon, space, "
     "integer), any order.",
     _verify_freq),
    ("longest",
     "Find the single longest word in words.txt and write just that word "
     "(lowercase, nothing else) to longest.txt.",
     _verify_longest),
]

PROMPT_SUFFIX = """

The word list is in words.txt (one word per line). Prefer building or reusing
tools in tools/ — check what's already there before writing new code.
mark_done when the output file is written and correct.
"""


def read_lru(ws: Path) -> dict[str, float]:
    lru = read_py_literal(ws / "tools" / "_lru.py", "LRU")
    return dict(lru) if isinstance(lru, dict) else {}


def main():
    p = argparse.ArgumentParser(description="Measure tool reuse across related tasks.")
    p.add_argument("--model", type=str, default=None, help="LiteLLM model string")
    p.add_argument("--fresh", action="store_true",
                   help="Control mode: fresh workspace per task (no tool carryover)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Per-task subprocess timeout in seconds (default 600)")
    args = p.parse_args()

    preflight("tool_reuse.py")
    RUNS_DIR.mkdir(exist_ok=True)

    mode = "fresh" if args.fresh else "shared"
    base = RUNS_DIR / f"tool_reuse_{mode}"
    if base.exists():
        shutil.rmtree(base)

    results = []
    shared_ws = base / "ws"
    for name, task_text, verify in TASKS:
        ws = (base / f"ws_{name}") if args.fresh else shared_ws
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "words.txt").write_text("\n".join(WORDS) + "\n")
        prompt_path = ws / "prompt.md"
        prompt_path.write_text(task_text + PROMPT_SUFFIX)

        tools_before = list_tools(ws)
        lru_before = read_lru(ws)

        print(f"\n{'─' * 50}\n▶ {name}  (workspace: {mode}, "
              f"{len(tools_before)} tool(s) on the shelf)")
        try:
            rc, elapsed, tail = run_ralph(ws, prompt_path, args.model,
                                          timeout=args.timeout,
                                          extra_args=["--exit-on-done"])
        except subprocess.TimeoutExpired:
            rc, elapsed, tail = -1, float(args.timeout), "(timeout)"

        lru_after = read_lru(ws)
        tools_after = list_tools(ws)
        created = sorted(tools_after - tools_before)
        # A pre-existing tool whose LRU timestamp advanced was CALLED this task.
        reused = sorted(
            t for t in tools_before
            if lru_after.get(t, 0.0) > lru_before.get(t, 0.0)
        )

        passed, reason = verify(ws)
        results.append({"task": name, "passed": passed, "elapsed": elapsed,
                        "created": created, "reused": reused, "reason": reason})
        symbol = "✅" if passed else "❌"
        print(f"{symbol} {name} ({elapsed:.1f}s)  created={created or '—'}  reused={reused or '—'}")
        if not passed:
            print(f"  reason: {reason or f'rc={rc}'}")

    total = sum(r["elapsed"] for r in results)
    n_pass = sum(r["passed"] for r in results)
    n_reuse = sum(bool(r["reused"]) for r in results)
    print(f"\n{'=' * 50}")
    print(f"Mode: {mode}    Passed: {n_pass}/{len(results)}    Total: {total:.0f}s")
    print(f"Tasks that reused a prior tool: {n_reuse}/{len(results)}")
    if not args.fresh:
        print("Run again with --fresh to get the no-carryover control numbers.")
    print(f"{'=' * 50}")
    for r in results:
        symbol = "✅" if r["passed"] else "❌"
        print(f"  {symbol} {r['task']:<16} {r['elapsed']:>6.1f}s  "
              f"created={len(r['created'])}  reused={len(r['reused'])}")


if __name__ == "__main__":
    main()
