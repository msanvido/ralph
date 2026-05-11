# Ralph

> "Everything is awesome — when it's all Python!"

A coding agent that re-feeds the same prompt every iteration until it marks the task
done. Each iteration starts with a fresh context — the agent rediscovers progress by
reading files in the workspace.

Named after [Wreck-It Ralph](https://en.wikipedia.org/wiki/Wreck-It_Ralph). Themed after
["Everything Is Awesome"](https://en.wikipedia.org/wiki/Everything_Is_Awesome) because,
well — everything here is Python.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

This installs Ralph and exposes a `ralph` command on your `PATH` (inside the venv).

Drop API keys for whichever providers you want into `.env`:

```ini
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=sk-or-...
```

## Models / providers

Ralph uses [LiteLLM](https://docs.litellm.ai), so any provider it supports works behind the
same `--model` flag. Examples:

```sh
.venv/bin/ralph --model openrouter/qwen/qwen3-coder              # default
.venv/bin/ralph --model openrouter/deepseek/deepseek-chat-v3.1
.venv/bin/ralph --model anthropic/claude-sonnet-4-6
.venv/bin/ralph --model openai/gpt-4o
.venv/bin/ralph --model gemini/gemini-2.0-flash
```

You can mix providers across Ralphs in the demo — e.g. solver on Anthropic, watcher on
Gemini. They only need to share `--bus-dir`, not the provider.

## Run one Ralph

```sh
echo "Build a tic-tac-toe game with tests, then mark_done." > prompt.md
.venv/bin/ralph
```

That's it. Ralph reads `./prompt.md`, works in `./workspace/`, learns into `./workspace/memory/`,
and joins the bus at `./bus/` with id = workspace dir name (`workspace`).

## Examples

A few ready-to-run demos live in `examples/`:

- `examples/solo.sh` — single Ralph implements Conway's Game of Life. The simplest
  demo of the iterate-until-done loop.
- `examples/sudoku.sh` — two Ralphs (expert + novice). The expert builds a solver
  tool + lessons; the novice fetches the solver via `ask_ralph` to crack a puzzle.
- `examples/cipher.sh` — two Ralphs (cryptanalyst + decoder). The cryptanalyst
  builds three Caesar-cipher tools (encode, decode, brute-force); the decoder picks
  the right one via `ask_ralph(..., description="...")` and recovers an unknown-shift
  ciphertext.
- `examples/multi_expert.sh` — four Ralphs where the novice must route asks to the
  right specialist based on each peer's published `expertise`. Showcases
  `ask_ralph(id, category, description)` and the top-3 description-match ranking.

Each script prefixes per-Ralph output by id and tees to `logs/<id>.log`; Ctrl-C
stops everything. Each example uses its own bus dir (`./bus_solo`, `./bus_sudoku`,
`./bus_multi`) so they don't interfere if you run more than one. Edit
`ws_*/prompt.md` after the first run to customize the prompts.

## Run multiple Ralphs manually

The rest of this section explains what `examples/sudoku.sh` does manually, in case
you want to run each Ralph in its own terminal.

The bus is a shared directory. Every Ralph that points at the same `--bus-dir` can see and
message every other Ralph. Give each one its own workspace and prompt, and a unique id.

**Terminal 1** — an expert Ralph building sudoku expertise (solver tool + recipes):

```sh
mkdir -p ws_expert && echo "Build a sudoku solver in tools/sudoku_solver.py and capture recipes in your memory. Mark_done when the solver passes tests and you have at least one recipe." > ws_expert/prompt.md
.venv/bin/ralph \
  --workspace ws_expert \
  --prompt ws_expert/prompt.md \
  --bus-dir ./bus \
  --ralph-id expert
```

**Terminal 2** — a novice Ralph that asks the expert for help:

```sh
mkdir -p ws_novice && echo "Solve this sudoku: 500080049000500030067300001150000000000208000000000018700004150030002000490050003. ask_ralph(id='expert', category='tools', description='sudoku solver') to fetch their solver — returns a request_id; check_response(request_id) on the next turn. Save the expert's source under tools/<filename>.py via write, then call the freshly-loaded solver. Save the answer to solution.txt and mark_done." > ws_novice/prompt.md
.venv/bin/ralph \
  --workspace ws_novice \
  --prompt ws_novice/prompt.md \
  --bus-dir ./bus \
  --ralph-id novice
```

Two Ralphs, one shared `./bus/` directory. They discover each other automatically.

Add more peers the same way — e.g., a watcher Ralph that just `list_ralphs()` /
`peek_ralph(id)` each iteration and writes a dashboard. See `examples/multi_expert.sh`
for a 4-Ralph setup with a watcher.

## What's available on the bus

Each Ralph sees these tools:

| Tool | Purpose |
|---|---|
| `list_ralphs()` | IDs of all Ralphs that have written a status file |
| `peek_ralph(id)` | Last status snapshot of another Ralph (iteration, last_text, done) |
| `ask_ralph(id, category, description)` | Fetch a peer's filesystem artifacts. `category` ∈ `tools \| recipes`. `description` is a short phrase saying what you need help with — the peer ranks their artifacts by keyword overlap with that description and returns the top 3 most relevant. Returns immediately with a `request_id`. On `check_response`, `tools` bundles are auto-installed into the requester's `workspace/tools/` verbatim (no LLM round-trip through the source), and the response surfaces `{installed_tools: [...]}`. `recipes` return `{answer: {lessons: [{id, description, prompt}, ...]}}` unchanged. |
| `check_response(request_id)` | Returns `{status: "pending"}` or the answer payload |

Peers can only share what they have **stored on their own filesystem**: their dynamic
tools (`workspace/tools/*.py`) and their memory lessons (`workspace/memory/<category>/*.py`).
The receiver's bus auto-fulfills the request — there is no manual respond step and no LLM
round-trip on the responder side.

**Auto-discovery.** Each Ralph publishes its current inventory (declared *expertise* +
tool filenames + per-category lesson descriptions) to `bus/<id>.status` after every
iteration. The expertise is a one-line phrase auto-extracted from the Ralph's prompt at
startup, so peers know what each Ralph specializes in. At the start of every iteration,
every other Ralph's status is collected and prepended to the prompt as a `## Peers on
the bus` section. The system prompt's first step is "route asks by expertise — if a peer
specializes in what you need, fetch from them via `ask_ralph(id, category, description)`
before writing your own." Reuse beats reinvention.

Memory layout (one file per recipe, mirroring the per-tool pattern):

```
workspace/memory/
└── recipes/
    ├── _index.py                  # INDEX = [{id, description}, ...]
    ├── always_ls_before_read.py   # MEMORY = {description, prompt}
    └── ...
```

Recipes are procedural/heuristic knowledge — strategies, decision heuristics, judgment
calls — for problems that don't reduce cleanly to a Python tool. Anything algorithmic
belongs in `workspace/tools/` as code, not here as text.

`description` is short — used for relevance selection (locally and when ranking against
a peer's `ask_ralph` request). `prompt` is the full recipe — loaded into context only
when selected. `ask_ralph(id, "recipes", description)` returns the top 3
`{id, description, prompt}` triples by keyword overlap with the requester's description.

## Bus directory layout

```
bus/
├── solver.fifo         # named pipe; receives request_ids
├── solver.status       # JSON snapshot of solver's last iteration
├── cli.fifo
├── cli.status
├── req/<rid>.json      # full request payloads
└── resp/<rid>.json     # full response payloads
```

FIFOs are POSIX-only (macOS/Linux). `req/` and `resp/` accumulate; sweep them between runs
if needed.

## Lifecycle: done → idle → resume

When Ralph calls `mark_done`, the loop **doesn't exit** — it enters an idle phase:

- The bus listener keeps running, so peers can still `ask_ralph(...)` you for tools or
  memory and the request will be auto-fulfilled from your filesystem.
- The main thread polls the `DONE` marker every second.
- **To wake Ralph up again**, edit the prompt (optional) and delete `workspace/DONE`. Ralph
  re-reads the prompt from disk and starts a fresh iteration count.

Ctrl-C still exits cleanly from either phase.

## Memory and tool caps (LRU)

Both stores are capped to keep context and the workspace bounded:

- **Tools** in `workspace/tools/*.py` — capped at 100. Touched on each call. When over the
  cap, the least-recently-used tool files are deleted on the next reload.
- **Memory** lessons across `workspace/memory/*/*.py` — capped at 100 total. Touched when
  added and when selected into the iteration prompt. Oldest lessons are deleted on add.

Last-used timestamps live in `workspace/tools/_lru.py` and `workspace/memory/_lru.py`.
Caps are module-level (`tools.TOOLS_LRU_LIMIT`, `memory.MEMORY_LRU_LIMIT`) — edit if you
want a different ceiling.

## CLI flags

```
--prompt PATH       Prompt file (default: ./prompt.md)
--workspace PATH    Working dir (default: ./workspace)
--bus-dir PATH      Shared bus dir (default: ./bus)
--ralph-id NAME     Identity on the bus (default: workspace dir name)
--model NAME        LiteLLM model string (default: openrouter/qwen/qwen3-coder).
                    The model MUST support forced tool_choice — Ralph's memory
                    and expertise extraction rely on it. Validated at startup.
```

## Tests

```sh
.venv/bin/python -m unittest test_ralph
```
