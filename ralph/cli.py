"""
Ralph
Re-feeds the same prompt to a coding agent every iteration until it marks the task done.
Each iteration starts with a fresh context — the agent sees prior progress only via files
on disk in workspace/. Lessons distilled from past iterations are loaded selectively each
turn (memory.py). Ralphs can ask each other for help over a shared file/FIFO bus (bus.py).

Multi-provider via LiteLLM: --model anthropic/... | openai/... | gemini/... | openrouter/...
"""
import argparse
import json
import sys
import time
from pathlib import Path

import litellm

from . import config
from . import memory
from . import tools as tools_mod
from .bus import Bus

SYSTEM_PROMPT = """You are Ralph (yes, the Wreck-It one). You wreck empty workspaces into working
code, one iteration at a time. Everything you build is Python, and Python is awesome.

Each iteration is a fresh context — your prior reasoning is gone, but your work persists in workspace/.
Lessons from past iterations are prepended to your prompt under "## Memory" — read them before acting.

Workflow every iteration:
1. CHECK PEERS FIRST. Your prompt starts with "## Peers on the bus" listing every other ralph,
   their declared *expertise*, and what they've built (tool filenames + recipe descriptions). Route
   asks by expertise: if a peer is the sudoku specialist and you need a sudoku solver, ask them —
   not the web-scraping ralph. Use ask_ralph(id, category, description), where:
     id          — the peer most likely to help (look at their expertise line)
     category    — "tools" (their Python tool files) or "recipes" (their procedural knowledge)
     description — a short phrase saying what you're trying to do; the peer ranks their artifacts
                   against this and sends back the top 3 most relevant (not everything they have)
   ask_ralph returns a request_id immediately. On the NEXT turn, call check_response(request_id)
   to retrieve the artifacts. For "tools" responses, the files are AUTO-INSTALLED into your own
   tools/ directory verbatim — you do NOT need to call `write` yourself. The loop reloads tools
   after any change, so the peer's tool becomes callable on your very next tool_use. For
   "recipes" the response carries {lessons: [...]} to read and apply.
2. Call ls (recursive=true the first time) to see prior progress in your own workspace.
3. Call read on relevant files to recover context. Use grep/find to locate code.
4. Make incremental progress with edit (surgical) or write (whole-file).
5. You can grow your own toolbelt by writing Python files into tools/ (paths are relative to your
   workspace root — use "tools/foo.py", NOT "workspace/tools/foo.py"). The loop reloads them after
   every tool call, so a tool you write becomes callable on the very next tool_use, even within
   the same response. Each file must define:
     TOOL = {"name": "...", "description": "...", "input_schema": {...}}
     def run(**kwargs) -> str | dict: ...
   The `run` function executes in Ralph's process; its return value is sent back as the tool
   result (dicts are JSON-encoded). Tool names must be unique and may not shadow the built-ins.
6. When every part of the task is fully complete, call mark_done.

Stay terse and in character. Everything is awesome."""


PEER_PREAMBLE_DESCRIPTIONS_PER_CAT = 10
PEER_ASK_TOP_K = 3  # max tools/lessons returned per ask_ralph, ranked by description match


def list_my_tools() -> list[str]:
    return [p.name for p in tools_mod.user_tool_files()]


def list_my_memory() -> dict:
    return {
        cat: [{"id": e.get("id"), "description": e.get("description")} for e in memory.load_index(cat)]
        for cat in memory.MEMORY_CATEGORIES
    }


def format_peers_preamble() -> str:
    """Inspect the bus and return a markdown section listing every other ralph + their inventory."""
    if config.bus is None:
        return ""
    me = config.bus.id
    peers = [r for r in config.bus.list_ralphs() if r != me]
    if not peers:
        return ""
    sections: list[str] = []
    for pid in peers:
        status = config.bus.peek_ralph(pid)
        if not isinstance(status, dict) or "error" in status:
            continue
        lines = [f"### {pid}"]
        expertise = (status.get("expertise") or "").strip()
        if expertise:
            lines.append(f"  expertise: {expertise}")
        lines.append(f"  iteration={status.get('iteration')}, done={status.get('done')}")
        if status.get("last_text"):
            lines.append(f"  last: {status['last_text']}")
        tools = status.get("tools") or []
        if tools:
            lines.append(f"  tools: {', '.join(tools)}")
        for cat, items in (status.get("memory") or {}).items():
            if not items:
                continue
            descriptions = [it.get("description", "") for it in items[:PEER_PREAMBLE_DESCRIPTIONS_PER_CAT]]
            tail = f" (+{len(items) - PEER_PREAMBLE_DESCRIPTIONS_PER_CAT} more)" if len(items) > PEER_PREAMBLE_DESCRIPTIONS_PER_CAT else ""
            lines.append(f"  {cat}: {len(items)} — {'; '.join(d for d in descriptions if d)}{tail}")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return (
        "## Peers on the bus\n\n"
        "Other ralphs are reachable. Route asks by `expertise` — if a peer specializes in what"
        " you need, fetch from THEM first. Use ask_ralph(id, category, description) where the"
        " description names what you're trying to do; the peer returns its top 3 matches.\n\n"
        + "\n\n".join(sections) + "\n\n---\n\n"
    )


def _publish_status(iteration: int, last_text: str = "", done: bool = False) -> None:
    if config.bus is None:
        return
    config.bus.write_status({
        "id": config.bus.id,
        "iteration": iteration,
        "last_text": last_text,
        "done": done,
        "ts": time.time(),
        "expertise": config.EXPERTISE,
        "tools": list_my_tools(),
        "memory": list_my_memory(),
    })


def fulfill_peer_request(category: str, request: dict) -> dict:
    """Auto-respond to a peer's ask by reading our own artifacts. Runs on the bus listener thread.
    Ranks artifacts by keyword overlap with the requester's `description` and returns the top K
    (PEER_ASK_TOP_K). When fewer than K items exist, returns all of them regardless of score."""
    query = (request.get("description") or "").strip()
    if category == "tools":
        scored: list[tuple[int, Path, str]] = []
        for path, tool_def in tools_mod.iter_user_tools():
            haystack = f"{tool_def.get('name', path.stem)} {tool_def.get('description', '')}"
            scored.append((tools_mod.score_keyword_overlap(query, haystack), path, path.name))
        scored.sort(key=lambda x: (-x[0], x[2]))
        top = scored[:PEER_ASK_TOP_K]
        return {"files": {name: path.read_text() for _score, path, name in top}}
    if category in memory.MEMORY_CATEGORIES:
        scored_lessons: list[tuple[int, str, str]] = []
        for entry in memory.load_index(category):
            description = entry.get("description", "")
            score = tools_mod.score_keyword_overlap(query, description)
            scored_lessons.append((score, entry["id"], description))
        scored_lessons.sort(key=lambda x: (-x[0], x[1]))
        lessons = []
        for _score, lesson_id, _desc in scored_lessons[:PEER_ASK_TOP_K]:
            lesson = memory.load_lesson(category, lesson_id)
            if lesson:
                lessons.append({
                    "id": lesson_id,
                    "description": lesson.get("description", ""),
                    "prompt": lesson.get("prompt", ""),
                })
        return {"lessons": lessons}
    return {
        "error": f"unknown category {category!r}; valid: {list(tools_mod.PEER_SHARE_CATEGORIES)}"
    }


def _last_assistant_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "assistant" and m.get("content"):
            return str(m["content"]).strip()[:200]
    return ""


def _msg_to_dict(msg) -> dict:
    """Convert a LiteLLM Message to a dict for the next call."""
    out: dict = {"role": msg.role, "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return out


def run_iteration(prompt: str, n: int) -> list[dict]:
    print(f"\n{'=' * 50}\n🔁 Iteration {n}\n{'=' * 50}")

    prompt = format_peers_preamble() + memory.select_relevant_memories(prompt) + prompt

    seen_names: set[str] = set()
    seen_warnings: set[str] = set()

    def refresh_tools() -> tuple[list[dict], dict]:
        schemas, dispatch, warnings = tools_mod.load_dynamic_tools()
        new_names = set(dispatch) - seen_names
        if new_names:
            print(f"  🧰 dynamic tools available: {', '.join(sorted(new_names))}")
            seen_names.update(new_names)
        for w in warnings:
            if w not in seen_warnings:
                print(f"  ⚠️  {w}")
                seen_warnings.add(w)
        all_tools = tools_mod.BUILTIN_TOOLS_OPENAI + [tools_mod.to_openai_tool(t) for t in schemas]
        return all_tools, dispatch

    iteration_tools, dynamic_dispatch = refresh_tools()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    while True:
        response = config.completion(
            model=config.MODEL,
            max_tokens=8192,
            messages=messages,
            tools=iteration_tools,
        )
        choice = response.choices[0]
        msg = choice.message

        if msg.content:
            print(f"🕹️  Ralph: {msg.content}")
        tool_calls = msg.tool_calls or []
        for tc in tool_calls:
            preview = tc.function.arguments or ""
            if len(preview) > 120:
                preview = preview[:117] + "..."
            print(f"  🔧 {tc.function.name}({preview})")

        messages.append(_msg_to_dict(msg))

        if not tool_calls or choice.finish_reason == "stop":
            break

        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or ""
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}
            result = tools_mod.handle_tool(name, args, dynamic_dispatch)
            preview = result if len(result) <= 200 else result[:197] + "..."
            print(f"  📎 {preview}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
            # Reload after any tool that could create or modify a dynamic tool file.
            # check_response is in here because tool bundles fetched from peers are
            # auto-installed into TOOLS_DIR — without a reload the model can't see
            # the new tool in its function list and tends to invent a wrapper call.
            if name in ("write", "edit", "check_response"):
                iteration_tools, dynamic_dispatch = refresh_tools()

    _publish_status(
        iteration=n,
        last_text=_last_assistant_text(messages),
        done=config.DONE_MARKER.exists(),
    )
    return messages


# Errors here mean the configured model/credentials can't possibly work —
# rate limits / network blips are NOT in this set and stay transient.
FATAL_MODEL_ERRORS = (
    litellm.BadRequestError,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.PermissionDeniedError,
)

# Used by validate_model. We force this tool to be called — that's the exact same
# pattern memory.py and extract_expertise rely on, so if the model can't handle it,
# Ralph won't work, period.
_PING_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Reply with the string 'pong'.",
        "parameters": {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
        },
    },
}


def validate_model() -> None:
    """Smoke-test the configured model with a forced-tool completion. Ralph's memory and
    expertise features rely on tool_choice={"type":"function","function":{"name":...}},
    so any model that rejects forced tool_choice (some OpenRouter endpoints do) is
    treated as fatal — we'd otherwise run amnesiac. Exits with code 1 on fatal errors;
    transient errors (rate limit, timeout) print a warning and let the main loop retry."""
    print(f"🔌 Checking model {config.MODEL!r}...", end=" ", flush=True)
    try:
        config.completion(
            model=config.MODEL,
            max_tokens=64,
            messages=[{"role": "user", "content": "Call ping with reply='pong'."}],
            tools=[_PING_TOOL],
            tool_choice={"type": "function", "function": {"name": "ping"}},
        )
    except FATAL_MODEL_ERRORS as e:
        print("FAIL")
        print(f"❌ {type(e).__name__}: {e}")
        print("   Ralph requires forced tool_choice support for memory + expertise.")
        print("   Check --model and your API keys in .env. Exiting.")
        sys.exit(1)
    except Exception as e:
        print(f"transient ({type(e).__name__}) — will retry in main loop")
        return
    print("OK")


def wait_for_peers(peer_ids: list[str], poll_seconds: float = 2.0) -> None:
    """Block until each named peer has published a status with done=True. Ctrl-C
    aborts the wait. Used to gate a 'novice' ralph behind a 'expert' that needs to
    finish building tools/lessons first. Must be called AFTER our own bus is up
    and our status is published — otherwise peers waiting on us would deadlock.

    A missing or unreadable status file is treated as 'not yet ready' rather than
    fatal: peers may start later, and they're allowed to."""
    if not peer_ids:
        return
    missing = list(peer_ids)
    print(f"⏸️  waiting for peer(s) to finish: {', '.join(missing)}")
    while missing:
        ready_now: list[str] = []
        for pid in missing:
            status = config.bus.peek_ralph(pid)
            if isinstance(status, dict) and status.get("done") is True:
                ready_now.append(pid)
                print(f"  ✓ {pid} is done")
        for pid in ready_now:
            missing.remove(pid)
        if missing:
            time.sleep(poll_seconds)
    print("▶️  all peers ready — starting iterations")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ralph: a Python coding agent")
    p.add_argument("--prompt", type=Path, default=Path("prompt.md"),
                   help="Path to the prompt file (default: ./prompt.md)")
    p.add_argument("--workspace", type=Path, default=Path("workspace"),
                   help="Working directory for Ralph's files (default: ./workspace)")
    p.add_argument("--bus-dir", type=Path, default=Path("bus"),
                   help="Shared bus directory for inter-Ralph pipes (default: ./bus)")
    p.add_argument("--ralph-id", type=str, default=None,
                   help="This Ralph's id on the bus (default: workspace dir name)")
    p.add_argument("--wait-for-peer", action="append", default=[], metavar="ID",
                   help="Block until peer ID has published status with done=true before "
                        "starting the iteration loop. Repeatable. Useful for novice ralphs "
                        "that should not act until an expert has finished building tools "
                        "and lessons (e.g., --wait-for-peer expert).")
    p.add_argument("--model", type=str, default=None,
                   help=f"LiteLLM model string. Default: {config.MODEL!r}. "
                        "Examples: openrouter/deepseek/deepseek-chat-v3.1, "
                        "openrouter/qwen/qwen3-coder, anthropic/claude-sonnet-4-6, "
                        "openai/gpt-4o, gemini/gemini-2.0-flash")
    return p.parse_args(argv)


def main():
    args = parse_args()
    config.WORKSPACE = args.workspace
    config.PROMPT_FILE = args.prompt
    config.TOOLS_DIR = config.WORKSPACE / "tools"
    config.MEMORY_DIR = config.WORKSPACE / "memory"
    config.DONE_MARKER = config.WORKSPACE / "DONE"
    if args.model:
        config.MODEL = args.model

    if not config.PROMPT_FILE.exists():
        print(f"Prompt file not found: {config.PROMPT_FILE.resolve()}")
        print("Pass --prompt <path> or create ./prompt.md, then re-run.")
        return

    config.WORKSPACE.mkdir(parents=True, exist_ok=True)
    config.TOOLS_DIR.mkdir(exist_ok=True)
    config.MEMORY_DIR.mkdir(exist_ok=True)
    if config.DONE_MARKER.exists():
        config.DONE_MARKER.unlink()

    print("=" * 50)
    print("  🕹️  Ralph 🕹️ ")
    print('  "Everything is awesome — when it\'s all Python!"')
    print("=" * 50)
    print(f"🤖 Model:     {config.MODEL}")
    print(f"📝 Prompt:    {config.PROMPT_FILE.resolve()}")
    print(f"📁 Workspace: {config.WORKSPACE.resolve()}")

    # Fail fast on misconfigured model/credentials, BEFORE touching the bus —
    # otherwise peers would see a ghost ralph that immediately crashes.
    validate_model()

    # Summarize our expertise so peers know what to ask us about. Best-effort: on failure
    # config.EXPERTISE stays empty and the preamble simply omits the expertise line.
    config.EXPERTISE = memory.extract_expertise(config.PROMPT_FILE.read_text())
    if config.EXPERTISE:
        print(f"🎓 Expertise: {config.EXPERTISE}")

    ralph_id = args.ralph_id or args.workspace.resolve().name
    config.bus = Bus(args.bus_dir, ralph_id, fulfill_request=fulfill_peer_request)
    # Publish presence + current inventory so peers can discover us before our first iteration.
    _publish_status(iteration=0)
    print(f"🚌 Bus:       {args.bus_dir.resolve()} (id={ralph_id})")

    # If --wait-for-peer was set, block here until each named peer is done.
    # Our bus is already up so peers can still see/ask us during the wait.
    try:
        wait_for_peers(args.wait_for_peer)
    except KeyboardInterrupt:
        print("\n🕹️  Ralph: Game over, man.")
        config.bus.close()
        return

    try:
        while True:
            iterate_until_done()
            if not config.PROMPT_FILE.exists():
                print(f"\nPrompt file vanished: {config.PROMPT_FILE}. Stopping.")
                return
            wait_until_done_removed()
    except KeyboardInterrupt:
        print("\n🕹️  Ralph: Game over, man.")
    finally:
        config.bus.close()


def iterate_until_done() -> None:
    """Inner loop: iterate until DONE_MARKER is created or MAX_ITERATIONS is hit."""
    for i in range(1, config.MAX_ITERATIONS + 1):
        prompt = config.PROMPT_FILE.read_text()
        messages = run_iteration(prompt, i)
        memory.learn_from_iteration(messages, i)
        if config.DONE_MARKER.exists():
            print(f"\n✅ Done after {i} iterations. Everything is awesome!")
            return
    print(f"\n⏱️  Reached max iterations ({config.MAX_ITERATIONS}) without DONE marker.")


def wait_until_done_removed(poll_seconds: float = 1.0) -> None:
    """Block while DONE_MARKER exists. Bus listener stays active (peer asks still auto-fulfill)."""
    print(
        f"\n🛌 Idle. Bus is still serving asks. Delete {config.DONE_MARKER}"
        " (and optionally edit the prompt first) to give Ralph a new task."
    )
    while config.DONE_MARKER.exists():
        time.sleep(poll_seconds)
    print("\n🕹️  Ralph: DONE removed — back to wrecking!")
    _publish_status(iteration=0, done=False)


if __name__ == "__main__":
    main()
