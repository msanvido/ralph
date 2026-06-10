# Ralph

> "Everything is awesome — when it's all Python!"

A coding agent that treats **tool creation** and **tool sharing** as first-class primitives. Ralph re-feeds the same prompt every iteration until it marks the task done; each iteration starts with a fresh context and rediscovers progress by reading the workspace.

Inspired by [fast-rlm](https://github.com/avbiswas/fast-rlm). Named after [Wreck-It Ralph](https://en.wikipedia.org/wiki/Wreck-It_Ralph). Themed after ["Everything Is Awesome"](https://en.wikipedia.org/wiki/Everything_Is_Awesome) because, well — everything here is Python.

## Why this design

What made humans dominant wasn't raw intelligence — it was the *combination* of two abilities: we **build tools**, and we **share them**. A single hominid who knaps a flint blade is a curiosity. A tribe that knaps blades, passes the technique forward, and trades them with neighbors is an evolutionary force.

AI systems have been climbing the same ladder, one rung at a time:

1. **Code generation.** Mostly mastered.
2. **Function calling with pre-defined tools.** Mostly mastered — [Hermes](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1) and friends made structured tool invocation reliable.
3. **Tool *creation* on demand.** Emerging — [ToolMaker](https://arxiv.org/abs/2502.11705) and related work showed agents can author their own Python tools mid-task.

No system to date has put tool creation **and** tool sharing together as first-class primitives. Tools are still treated as private artifacts of a single agent — built ad hoc, used once, lost when the conversation ends. Ralph's bet: make both operations primitive. Tools are `.py` files in the agent's workspace. Peers can request tools by description over a shared bus, and the source code transfers verbatim with no LLM round-trip on the responder side. The substrate that holds the tools is the agent's own filesystem.

### Where Ralph sits in the lineage

- **ReAct / CodeReAct** — reason, act, observe; tools are the actions. Usually pre-defined; if built, they live inside one rollout.
- **[Recursive Language Models](https://arxiv.org/abs/2512.24601)** (Zhang, Kraska, Khattab, 2025) — treat the prompt as a Python variable inside a REPL the LLM controls. Scales context past the window in one inference.
- **Ralph** — brings the tools themselves *inside* the agent's substrate. State, tools, and recipes are all `.py` files on disk. Other agents can request those files by description and receive verbatim source.

The shift is from "the LLM calls tools" to "the LLM lives in a workshop, builds tools, files them on a shelf, and borrows from the shelf next door."

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

### Tool creation, live

Single Ralph, no peers. Prompt: solve one sudoku puzzle.

```
🔁 Iteration 1
🕹️  I'll build a backtracking sudoku solver and use it.
  🔧 write({"path": "tools/sudoku_solver.py", ...})
  📎 {"status": "written", "bytes": 2069}
  🧰 dynamic tools available: sudoku_solver
  🔧 sudoku_solver({"puzzle": "003020600...300"})
  📎 483921657967345821251876493548132976...382
  🔧 write({"path": "solution.txt", "content": "483921657..."})
  🔧 mark_done({})
✅ Done after 1 iterations.
```

Ralph wrote a tool, the harness reloaded `workspace/tools/` so the new tool was callable on the very next turn, the tool ran, the answer was saved. Verified — all clues preserved, rows / columns / 3×3 boxes valid. No `import` ever happened; the `sudoku_solver` came into existence because the agent needed it, lives as `tools/sudoku_solver.py`, and will be LRU-evicted from the workspace once it falls below the cap.

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
- `examples/parallel_r_count.sh` — four Ralphs: three list specialists (fruits,
  animals, US states) each expose a `list_*` tool; an aggregator fetches all
  three via `ask_ralph`, calls them, and tallies 'r' occurrences into
  `solution.json`. Ralph's analog of fast-rlm's asyncio.gather example.
- `examples/podcast.sh` — single Ralph reading the Lex Fridman podcast
  transcripts CSV (downloaded from Kaggle into the workspace) to summarize
  what the first 5 ML guests said about AGI. Long-context exploration via
  grep/read — ported from fast-rlm.

Each script prefixes per-Ralph output by id and tees to `logs/<id>.log`; Ctrl-C
stops everything. Each example uses its own bus dir (`./bus_solo`, `./bus_sudoku`,
`./bus_multi`, `./bus_rcount`, `./bus_podcast`) so they don't interfere if you
run more than one. Edit `ws_*/prompt.md` after the first run to customize the
prompts.

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

### Tool sharing, live

Two Ralphs share a `bus/` directory. The expert builds a sudoku solver and marks done. The novice gets a fresh puzzle and is told to ask the expert.

```
🔁 Iteration 1 (novice)
🕹️  Getting the solver from the expert peer.
  🔧 ask_ralph({"id": "expert", "category": "tools",
                "description": "sudoku solver"})
  📎 {"request_id": "ec4571baa730", "status": "sent"}
```

The expert's bus daemon — a background thread, no LLM round-trip — ranks its own filesystem artifacts by keyword overlap with `"sudoku solver"`, picks the top match, and writes the response. The response is the *verbatim source* of `tools/sudoku_solver.py`. The novice's `check_response` auto-installs it into its own `workspace/tools/` on the next turn. Two agents, one file transferred over a shared directory, no central server, no package manager.

When the expert is offline, the bus fails gracefully and the agent falls back to building its own:

```
  🔧 ask_ralph({"id": "expert", ...})
  📎 {"error": "target unreachable: ... 'bus/expert.fifo'"}
🕹️  Expert isn't reachable. No problem — I'll build my own.
  🔧 write({"path": "tools/sudoku_solver.py", ...})
  🔧 sudoku_solver({"puzzle": "500080049..."})
  📎 513687249849521637...723
✅ Done after 1 iterations.
```

"Find help, or become the help." The next Ralph to attack a similar problem will see *this* one on the bus and can borrow from it. Tools accumulate the way blades accumulated in a flint-knapping village.

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

## Storage is Python source

Tools are `.py` files with a `TOOL = {...}` dict (a pure literal) and a `run()` function. Recipes (heuristic memory) are `.py` files with a `MEMORY = {...}` dict. Indices are `INDEX = [...]`. The loader follows an execute/parse split: metadata is read via `ast.literal_eval` without ever executing the file (so ranking and sharing tools on the bus can't run anyone's top-level code), while `run()` is executed only when the tool is loaded for calling. The persisted form is the same Python objects the agent works with, frozen to text — readable with `cat`, diffable in git, hand-editable, runnable. No JSON, no pickle, no SQLite, no vector database. Sharing reduces to copying a file; verification reduces to reading it.

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
--max-iterations N  Stop after N iterations without a DONE marker (default: 50)
--exit-on-done      Exit when the task completes instead of idling on the bus.
                    Exit code 0 = mark_done was called; 2 = max iterations hit
                    without one. Used by the benchmark runners.
```

## Benchmarks

Five bundled evaluations — see [`benchmark/README.md`](benchmark/README.md) for details:

- **HumanEval subset** (`run.py`, 16 tasks) — raw capability, end-to-end through the loop. Graded on two axes: *code correct* and *mark_done called*, because they fail independently.
- **Tool reuse** (`tool_reuse.py`) — five related tasks against one shared workspace; measures whether tools built early actually get *called* later (via LRU timestamps), with a fresh-workspace control (`--fresh`).
- **Multi-Ralph sharing** (`multi_ralph.py`) — novice time-to-solution on a sudoku with vs without an expert on the bus, solution machine-verified, tool transfer confirmed.
- **LongBench narrativeqa / oolong-synth** (`longbench.py`, `oolong_synth.py`) — long-context navigation via grep/read, ported from [fast-rlm](https://github.com/avbiswas/fast-rlm).

A representative observation from an early run: on `HumanEval/34` with `claude-haiku-4-5`, Ralph wrote a correct one-line solution (`return sorted(set(l))`) — verified against the canonical HumanEval `check` — but never called `mark_done`. The code was right; the contract wasn't completed. The discipline gap is at the meta-protocol level (when to declare a task done), not at the toolmaking level — which is why the runner now reports "code correct but no mark_done" as its own category.

## Tests

```sh
.venv/bin/python -m unittest test_ralph
```

## Inspirations and related work

- **[fast-rlm](https://github.com/avbiswas/fast-rlm)** — the spark for this project. Several of Ralph's example workloads (`parallel_r_count`, `podcast`) and the long-context benchmarks (LongBench narrativeqa, oolong-synth) are ported from fast-rlm. fast-rlm is itself a fast, hackable Pythonic implementation of recursive language models — go read it.
- **[Geoffrey Huntley's "Ralph Wiggum" loop](https://ghuntley.com/ralph/)** — the observation that *just re-running the same prompt until done* captures a surprising amount of agentic work, no clever planner required. Ralph keeps this core and adds the surrounding machinery: dynamic tools, distilled recipes, peer cooperation over a bus, expertise routing, fail-fast model validation.
- **[Recursive Language Models](https://arxiv.org/abs/2512.24601)** (Zhang, Kraska, Khattab, 2025) — manipulating context symbolically through code rather than consuming it end-to-end. Ralph is the orthogonal sibling: code-as-substrate across iterations, where RLM is code-as-substrate within one inference. The two compose cleanly — `config.completion` could in principle be bound to `rlm.completion`.
- **[Hermes function-calling](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1)** and **[ToolMaker](https://arxiv.org/abs/2502.11705)** — pointing the way toward agentic tool authorship. Ralph extends the line by making *sharing* the newly-authored tools a runtime primitive, not a manual artifact-export step.
- **[Code as Agent Harness](https://arxiv.org/abs/2605.18747)** (Ning et al., 2026) — a survey of the shift from code-as-output to code-as-*substrate* in agent systems. Their tool-use taxonomy frames what Ralph does: agents that *author* reusable tools (in the lineage of Voyager's skill library and BOSS's skill chains) rather than merely calling fixed APIs. Ralph adds the dimension those systems lack — peer-to-peer tool transfer, where one agent's codified skill becomes another's callable tool without an LLM round-trip.
- **Coding agents (Claude Code, Cursor, Aider, OpenHands, …)** — solve the long-horizon coding-task problem with more sophisticated UIs and tool-use surfaces. Ralph is intentionally tiny — a few hundred lines you can read in one sitting — and keeps a few opinionated design choices the bigger systems generally don't: (1) every iteration is a *cold start* on the LLM side, with the filesystem as the only memory; (2) recipes are *Python source*, not a vector store; (3) peer cooperation is file-based, so a Ralph crashing or being `kill -9`'d doesn't break anyone else.
