# Ralph: A Tutorial

> "Everything is awesome — when it's all Python!"

A coding agent in ~1300 lines of Python. Named after Wreck-It Ralph (he wrecks empty
workspaces into working code). The interesting parts fit on one page.

This tutorial walks through the design from the outside in: the technique, then the
moving parts that implement it.

---

## 1. The technique

Most agent harnesses keep a growing message history. Each turn appends to it; the
agent reasons over the whole transcript. This works, but it has problems:

- The transcript grows monotonically — long tasks blow past context windows.
- A bad turn poisons every subsequent turn.
- Restarting means replaying the whole transcript.

Ralph takes the opposite stance: **every iteration is a brand-new conversation
with a fresh model context**. The agent's "memory" is whatever it left on disk
in `workspace/`. To continue work, the agent uses `ls` and `read` to rediscover
state from scratch. It doesn't remember what it tried before, but it sees what's
in the workspace right now — which is the only thing that actually matters for
"can I make progress next?"

```
┌─────────────────┐                 ┌─────────────────┐
│   Iteration N   │   ──same───▶    │  Iteration N+1  │
│  fresh context  │     prompt      │  fresh context  │
│  + workspace/   │                 │  + workspace/   │
│  + tools/       │                 │  + tools/       │
└─────────────────┘                 └─────────────────┘
       writes                              reads back
       to disk                             from disk
```

When the agent calls `mark_done`, the loop checks for `workspace/DONE` after each
iteration and exits if present.

That's it. Everything below is mechanism in service of this idea.

---

## 2. The minimum loop

The whole concept is in `ralph.run_iteration` and `ralph.iterate_until_done`:

```python
# ralph/cli.py — simplified outline
def iterate_until_done():
    for i in range(1, MAX_ITERATIONS + 1):
        prompt = PROMPT_FILE.read_text()    # re-read every iteration
        run_iteration(prompt, i)            # fresh context
        if DONE_MARKER.exists():
            return

def run_iteration(prompt, n):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    while True:
        response = litellm.completion(model=MODEL, messages=messages, tools=tools)
        msg = response.choices[0].message
        messages.append(_msg_to_dict(msg))
        if not msg.tool_calls:
            return
        for tc in msg.tool_calls:
            result = handle_tool(tc.function.name, json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
```

A standard tool-use loop. The crucial thing is what's *not* there: nothing carries
state into iteration N+1 except the filesystem.

See [`ralph/cli.py`](./ralph/cli.py) for the full implementation with multi-provider
support via [LiteLLM](https://docs.litellm.ai), pretty-printing, and a few extras.

---

## 3. File tools (the agent's hands)

Seven built-in tools, defined in [`tools.py`](./tools.py):

| Tool | Purpose |
|---|---|
| `ls(path=".", recursive=false)` | List directory contents |
| `read(path)` | Read a file |
| `write(path, content)` | Write a file (overwrites, creates parents) |
| `edit(path, old_string, new_string)` | Surgical replace; errors on ambiguity |
| `grep(pattern, path=".", glob=null)` | Regex over files |
| `find(name, path=".")` | Glob filename search |
| `mark_done()` | Signal completion (writes `workspace/DONE`) |

Every path is resolved via `safe_path` to prevent escaping `workspace/`:

```python
def safe_path(rel: str) -> Path:
    root = config.WORKSPACE.resolve()
    p = (config.WORKSPACE / rel).resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return p
```

This is the only sandboxing — the rest of the workspace is the agent's playground.

---

## 4. Self-extending toolbelt

Ralph can write his own tools. When he creates `workspace/tools/my_tool.py`
containing:

```python
TOOL = {
    "name": "my_tool",
    "description": "...",
    "input_schema": {"type": "object", "properties": {...}},
}

def run(**kwargs) -> dict:
    return {"result": ...}
```

…the loader (`tools.load_dynamic_tools`) picks it up, normalizes the schema to
OpenAI shape, and adds `my_tool` to the tools list. The reload happens after
every `write` or `edit`, so a tool Ralph writes mid-response is callable in the
very next `tool_use` of the same response.

The mechanism is just `importlib.util.spec_from_file_location`:

```python
# tools.py
def load_py_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module
```

Why this matters: complex sub-procedures get crystallized into reusable code
rather than re-derived each iteration. Ralph notices "I keep doing X" and
codifies X.

A capacity cap (`TOOLS_LRU_LIMIT = 100`) keeps the toolbelt bounded.
Least-recently-used tools get evicted on the next reload. Touch happens on call
via a thin wrapper:

```python
def _wrap_with_lru_touch(run_fn, filename: str):
    def handler(**kwargs):
        _touch_tool(filename)
        return run_fn(**kwargs)
    return handler
```

---

## 5. Memory: distilled lessons across iterations

The "fresh context every iteration" property has a cost: lessons learned in
iteration N are forgotten in iteration N+1 unless they're somehow surfaced again.
Tools handle this for *procedures* (a tool Ralph writes is always available),
but not for *advice* ("backtracking timed out, use MRV heuristic instead").

Memory ([`memory.py`](./memory.py)) fills that gap. After each iteration:

1. **Learn pass** — a forced-tool LLM call asks Ralph to extract durable lessons
   from the iteration transcript, in three buckets:
   - `worked` — moves that produced progress (positive reward)
   - `failed` — moves that wasted effort (negative reward)
   - `recipes` — reusable mini-procedures with steps

2. **Persist** — each lesson gets its own Python file:
   ```
   workspace/memory/
   ├── worked/
   │   ├── _index.py             # INDEX = [{id, description}, ...]
   │   ├── ls_before_read.py     # MEMORY = {description, prompt}
   │   └── ...
   ├── failed/
   └── recipes/
   ```

   `description` is short (used for selection); `prompt` is the full lesson
   loaded into context only when selected.

Before each iteration:

3. **Select pass** — another forced-tool LLM call shows Ralph the catalog of
   lesson descriptions and asks which are relevant to the current task. Only
   selected lessons' full prompts are loaded into the iteration prompt.

This lets memory grow large (cap at 100 lessons across categories) without
inflating the context for every task. Selection cost is one small LLM call per
iteration.

---

## 6. The bus: Ralphs cooperating

Multiple Ralphs can share a `--bus-dir` and discover each other.
[`bus.py`](./bus.py) implements peer-to-peer communication:

- `bus/<id>.fifo` — POSIX named pipe carrying request IDs (line-delimited)
- `bus/<id>.status` — JSON snapshot rewritten after each iteration
  (`iteration`, `done`, `last_text`, `tools`, `memory` description list)
- `bus/req/<rid>.json`, `bus/resp/<rid>.json` — request/response payloads

Each Ralph runs a daemon thread that reads its own FIFO. When a request
arrives, an auto-fulfillment callback (`ralph.fulfill_peer_request`) reads the
appropriate part of the local filesystem and writes a response — no LLM call,
no responder turn.

Tools available to Ralph:

| Tool | Purpose |
|---|---|
| `list_ralphs()` | IDs of all Ralphs visible on the bus |
| `peek_ralph(id)` | A peer's last status (mtime-cached) |
| `ask_ralph(id, category)` | Fetch from a peer. `category` ∈ `tools \| worked \| failed \| recipes`. Returns `{request_id}` immediately. |
| `check_response(request_id)` | `{status: "pending"}` or the response payload |

**Auto-discovery preamble.** At the top of every iteration, a `## Peers on the
bus` section is auto-prepended to the prompt, listing every other Ralph and
their inventory (tools + lesson descriptions). The system prompt's first step
is "if a peer has matching work, fetch theirs first." This is what turns
discovery from "Ralph might call `list_ralphs`" into "Ralph sees peers without
asking."

The trust model is cooperative: any process with filesystem access to
`./bus/` can write requests or impersonate. Fine for cooperating peers in the
same project, not safe across an untrusted boundary.

---

## 7. Lifecycle: done is a file, not a state

`mark_done` writes `workspace/DONE`. The outer loop polls for it after each
iteration:

```python
while True:
    iterate_until_done()           # iterate until DONE exists or MAX_ITERATIONS
    wait_until_done_removed()      # poll-until DONE is deleted
    # back to top — re-read prompt fresh
```

When `DONE` exists, the main thread sleeps. The bus listener thread keeps
running, so peers can still query this Ralph for tools/memory. Edit
`prompt.md` (optional), delete `workspace/DONE`, and Ralph wakes up with a
fresh iteration count and a re-read prompt.

Ctrl-C exits cleanly from either phase via a single top-level
`KeyboardInterrupt` handler.

---

## 8. Multi-provider via LiteLLM

The model is called through [LiteLLM](https://docs.litellm.ai), which accepts
the OpenAI-format messages/tools and translates per provider:

```sh
ralph --model openrouter/deepseek/deepseek-chat-v3.1   # default
ralph --model anthropic/claude-sonnet-4-6
ralph --model openai/gpt-4o
ralph --model gemini/gemini-2.0-flash
```

API keys come from `.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY`). The codebase uses OpenAI message
shape internally; tool schemas use OpenAI's `{type: "function", function:
{name, description, parameters}}` format. The loader normalizes the user-
facing dynamic-tool contract (`{name, description, input_schema}`) into that
shape on load.

---

## 9. Try it

```sh
pip install -e .
echo "OPENROUTER_API_KEY=..." > .env

# A simple task
echo "Write Conway's Game of Life in life.py with tests in test_life.py. Make tests pass, then mark_done." > prompt.md
ralph
```

Watch the workspace fill up:

```sh
ls workspace/
cat workspace/life.py
```

Multi-Ralph demo (one expert, one novice asking expert for help):

```sh
./start_demo.sh   # see start_demo.sh for the prompts it seeds
```

Benchmark on a HumanEval subset:

```sh
python benchmark/run.py            # all tasks
python benchmark/run.py --task HumanEval/0   # single task
```

---

## 10. What's not here

Ralph deliberately omits things you'll find in heavier agent frameworks:

- **No conversation persistence across iterations.** Re-derivation from disk is the design.
- **No vector memory.** Memory is small markdown-flavored Python files indexed by description.
- **No graph framework / state machine.** Just a re-feed loop.
- **No sub-agent delegation.** Cooperation is peer-to-peer over the bus, not parent/child.
- **No bash tool.** Ralph writes code; verification (e.g. running tests) happens externally.
  This is a deliberate constraint — it forces solutions to be correct-by-construction
  rather than verified-by-bash.

If you need any of these, fork it. The whole codebase fits in your head — that's
the point.

---

## 11. Read the source

In rough order of importance:

- [`ralph/cli.py`](./ralph/cli.py) — the loop, the system prompt, the entry point
- [`ralph/tools.py`](./ralph/tools.py) — file tools, dynamic loader, dispatch
- [`ralph/memory.py`](./ralph/memory.py) — lesson storage, select/learn passes
- [`ralph/bus.py`](./ralph/bus.py) — FIFO listener, auto-fulfillment, status files
- [`ralph/config.py`](./ralph/config.py) — paths, model, LiteLLM entry point
- [`test_ralph.py`](./test_ralph.py) — 69 tests covering each layer
