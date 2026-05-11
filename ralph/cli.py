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
import time
from pathlib import Path

from . import config
from . import memory
from . import tools as tools_mod
from .bus import Bus

SYSTEM_PROMPT = """You are Ralph (yes, the Wreck-It one). You wreck empty workspaces into working
code, one iteration at a time. Everything you build is Python, and Python is awesome.

Each iteration is a fresh context — your prior reasoning is gone, but your work persists in workspace/.
Lessons from past iterations are prepended to your prompt under "## Memory" — read them before acting.

Workflow every iteration:
1. CHECK PEERS FIRST. Your prompt starts with "## Peers on the bus" listing every other ralph
   and what they've built — their tool filenames and the descriptions of their lessons. If any
   peer has tools or recipes that match what you'd need to do, FETCH THEIRS before writing your
   own. Reuse > reinvent. Use ask_ralph(id, category) where category is:
     "tools"   — their dynamic tool source files (workspace/tools/*.py)
     "worked"  — their successful moves
     "failed"  — their failures
     "recipes" — their reusable mini-procedures
   ask_ralph returns a request_id immediately. On the NEXT turn, call check_response(request_id)
   to retrieve the artifacts. If "tools" came back, write each file's source to tools/<filename>
   via the write tool — the loop will load the new tool and you can call it on the next turn.
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
        "Other ralphs are reachable. If their tools or memory match your task, fetch theirs"
        " via ask_ralph(id, category) BEFORE writing your own. ask_ralph returns a request_id"
        " immediately; check_response(request_id) on a later turn returns the artifacts.\n\n"
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
        "tools": list_my_tools(),
        "memory": list_my_memory(),
    })


def fulfill_peer_request(category: str, request: dict) -> dict:
    """Auto-respond to a peer's ask by reading our own artifacts. Runs on the bus listener thread."""
    if category == "tools":
        return {"files": {p.name: p.read_text() for p in tools_mod.user_tool_files()}}
    if category in memory.MEMORY_CATEGORIES:
        lessons = []
        for entry in memory.load_index(category):
            lesson = memory.load_lesson(category, entry["id"])
            if lesson:
                lessons.append({
                    "id": entry["id"],
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
            # Reload only after operations that could create or modify a dynamic tool file.
            if name in ("write", "edit"):
                iteration_tools, dynamic_dispatch = refresh_tools()

    _publish_status(
        iteration=n,
        last_text=_last_assistant_text(messages),
        done=config.DONE_MARKER.exists(),
    )
    return messages


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

    ralph_id = args.ralph_id or args.workspace.resolve().name
    config.bus = Bus(args.bus_dir, ralph_id, fulfill_request=fulfill_peer_request)
    # Publish presence + current inventory so peers can discover us before our first iteration.
    _publish_status(iteration=0)

    print("=" * 50)
    print("  🕹️  Ralph 🕹️ ")
    print('  "Everything is awesome — when it\'s all Python!"')
    print("=" * 50)
    print(f"🤖 Model:     {config.MODEL}")
    print(f"📝 Prompt:    {config.PROMPT_FILE.resolve()}")
    print(f"📁 Workspace: {config.WORKSPACE.resolve()}")
    print(f"🚌 Bus:       {args.bus_dir.resolve()} (id={ralph_id})")

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
