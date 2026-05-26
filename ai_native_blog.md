# Tool creation and tool sharing as first-class citizens

What made humans dominant wasn't raw intelligence. It was the *combination* of two abilities: we **build tools**, and we **share them**. A single hominid who figures out how to knap a flint into a blade is a curiosity. A tribe that knaps blades, passes the technique to the next generation, and trades the blades with neighbors is an evolutionary force. The leap wasn't toolmaking alone — chimps make tools. It was toolmaking *plus* a social substrate that let a tool, once invented, propagate.

AI systems have been climbing the same ladder, one rung at a time:

1. **Code generation.** Mostly mastered. Pretrained models write competent Python.
2. **Function calling with pre-defined tools.** Mostly mastered. [Hermes](https://huggingface.co/datasets/NousResearch/hermes-function-calling-v1) and friends made structured tool invocation a reliable behavior, not a hallucination risk.
3. **Tool *creation* on demand.** Just emerging. Recent work — [ToolMaker](https://arxiv.org/abs/2502.11705), ATLASS, OpenClass-style agentic authorship — has shown that agents can author their own Python tools mid-task instead of calling a fixed library.

But no system to date has put tool **creation** and tool **sharing** together as first-class primitives of the runtime. Tools are still treated as private artifacts of a single agent — built ad hoc, used once, and lost when the conversation ends. The blades get knapped, then thrown in the river.

Ralph is an attempt to make that next leap. Tool creation *and* tool sharing are both core operations on the same equal footing as `write` and `read`. The substrate that holds the tools is the agent's own filesystem. Peers can ask each other for tools by description, and the tool — verbatim source code — moves across the bus without an LLM round-trip in between.

## Where Ralph sits in the lineage

- **ReAct / CodeReAct.** Reason, act, observe; tools are the actions. Tools are usually pre-defined; if they're built, they live inside one rollout.
- **Recursive Language Models** ([Zhang, Kraska, Khattab, 2025](https://arxiv.org/abs/2512.24601)). Treat the prompt as a Python variable inside a REPL the LLM controls. The LLM manipulates context *symbolically* through code, scaling a single inference past the context window.
- **Ralph.** Brings the tools themselves *inside* the agent's substrate — not as REPL variables, but as files in `workspace/tools/`. State, tools, and recipes are all `.py` files on disk. Other agents can request those files by description and receive verbatim source. Code is no longer an artifact of the run; it's the medium the agents live in and trade in.

The shift is from "the LLM calls tools" to "the LLM lives in a workshop, builds tools, files them on a shelf, and borrows from the shelf next door."

## What Ralph is, mechanically

Ralph is named after Wreck-It Ralph and shaped by [Geoffrey Huntley's "Ralph Wiggum" loop](https://ghuntley.com/ralph/) — the observation that a surprising amount of long-horizon work can be done by *just running the same prompt over and over* until it's done. The core is twelve lines of pseudocode:

```
while not Done(W) and i < MAX_ITERATIONS do
    context  ← PeersPreamble(B) ∥ SelectRelevantRecipes(W) ∥ ReadFile(P)
    msgs     ← [System(SYSTEM_PROMPT), User(context)]
    tools    ← Builtins ∪ LoadDynamicTools(W/tools)
    while True do
        msgs ← msgs ∥ LLM(msgs, tools)
        if no tool_calls(msgs[-1]) then break
        for tc in tool_calls(msgs[-1]) do
            (W, B, result) ← Exec(tc, W, B)
            msgs ← msgs ∥ Result(result)
            if tc.name ∈ {write, edit, check_response} then
                tools ← Builtins ∪ LoadDynamicTools(W/tools)
    LearnRecipes(msgs, W)
    PublishStatus(B, W)
```

Every iteration is a brand-new conversation. The agent's "memory" is whatever it left on disk in `workspace/`. To continue work, the next iteration runs `ls` and `read` to rediscover state from scratch. No growing transcript, no replay-from-scratch problem — just a workspace, a prompt, and an inventory of tools and recipes published to the bus for peers to see.

## Tool creation, live

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

## Tool sharing, live

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

## Source is the storage format

Tools are `.py` files with a `TOOL = {...}` dict and a `run()` function. Recipes (heuristic memory) are `.py` files with a `MEMORY = {...}` dict. Indices are `INDEX = [...]`. The loader `importlib`s them. The persisted form is the same Python objects the agent works with, frozen to text — readable with `cat`, diffable in git, hand-editable, runnable. No JSON, no pickle, no SQLite, no vector database. Sharing reduces to copying a file; verification reduces to reading it.

Both stores are LRU-capped. Tools and recipes that haven't been touched recently are deleted. The shelf is finite; the workshop stays tidy.

## Benchmark snapshot

End-to-end runs on a HumanEval subset (16 tasks), single-example LongBench narrativeqa, and oolong-synth. A representative result from this morning: on `HumanEval/34` with `claude-haiku-4-5`, Ralph wrote a correct one-line solution (`return sorted(set(l))`) — verified passing against the canonical HumanEval `check` — but never called `mark_done`, and the runner cut it off at 600s. The code was right; the contract wasn't completed. That failure mode is informative. It says the discipline gap is at the *meta-protocol* level (when to declare a task done), not at the toolmaking level. Aggregate pass@1 numbers on the full 16-task subset are running next.

## Why this matters

The bet underneath Ralph is that the right unit of AI capability is no longer the *model* and no longer the *toolset shipped with the model*. It's the *workshop* — the agent plus everything it has built and everything its peers have published. Code generation gave us models that can write code. Tool creation gave us agents that can extend themselves. Tool sharing, made first-class, is what lets capability *accumulate across runs and across agents*.

That's what evolution did with stone tools and language. It's plausibly what AI-native software will do with code.

The prototype is working. The benchmark pass is next.
