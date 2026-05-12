# Ralph: a coding agent in a few hundred lines

Most agent harnesses keep a growing message history. Each turn appends to it;
the agent reasons over the whole transcript. This works, but it has problems:
the transcript grows monotonically and blows past context windows; a bad turn
poisons every subsequent turn; restarting means replaying the whole transcript.

Ralph takes the opposite stance: **every iteration is a brand-new conversation
with a fresh model context**. The agent's "memory" is whatever it left on disk
in `workspace/`. To continue work, the agent uses `ls` and `read` to rediscover
state from scratch. It doesn't remember what it tried before, but it sees what's
in the workspace right now — which is the only thing that actually matters for
"can I make progress next?"

Multiple Ralphs can share a file/FIFO bus and discover each other automatically.
Each Ralph publishes its expertise (auto-extracted from its prompt at startup)
and its inventory of tools and recipes; peers route asks by expertise.

---

## The algorithm

```
Input:  prompt file P, workspace W, peer bus B
Output: artifacts in W, recipes in W/memory/recipes

while not Done(W) and i < MAX_ITERATIONS do
    i        ← i + 1
    context  ← PeersPreamble(B)  ∥  SelectRelevantRecipes(W)  ∥  ReadFile(P)
    msgs     ← [ System(SYSTEM_PROMPT), User(context) ]
    tools    ← Builtins  ∪  LoadDynamicTools(W/tools)

    while True do
        msgs ← msgs ∥ LLM(msgs, tools)
        if no tool_calls(msgs[-1]) then break
        for tc in tool_calls(msgs[-1]) do
            (W, B, result) ← Exec(tc, W, B)
            msgs           ← msgs ∥ Result(result)
            if tc.name ∈ {write, edit, check_response} then
                tools ← Builtins ∪ LoadDynamicTools(W/tools)   // reload

    LearnRecipes(msgs, W)        // distill → W/memory/recipes/*.py
    PublishStatus(B, W)          // surface inventory + expertise to peers
```

Three loops, nested:
1. The outer loop runs until `mark_done` writes `W/DONE`.
2. The inner loop is a standard tool-use loop within one fresh context.
3. The tool-reload step is implicit: any `write` / `edit` / `check_response`
   reloads `W/tools/` so a tool created mid-response is callable on the very
   next `tool_use`.

---

## Design notes

**Python source is the storage format.** State on disk isn't an opaque blob —
it's `.py` files. Dynamic tools are modules with `TOOL = {...}` and a `run()`
function. Recipes are files with `MEMORY = {...}`. Indices are
`INDEX = [...]`. The loader `importlib`s them, so the persisted form is the
same Python objects the agent works with, just frozen to text — readable,
diffable, hand-editable, runnable. No JSON, no pickle, no SQLite.

**Writes are eager.** Every tool the agent saves, every recipe the learn pass
extracts, every LRU touch hits disk the moment it's produced. There's no
"save on shutdown" or "flush at checkpoint" — a crash or `kill -9` loses
nothing already done. The price is more writes; the benefit is that the
entire state of the world is always on disk and always inspectable with `cat`.

**Tools vs. recipes.** Anything algorithmic gets codified as a tool (Python in
`W/tools/`). Anything procedural or heuristic — strategies, when-to-use-which,
hard-won judgment — becomes a recipe in `W/memory/recipes/`. Two artifacts,
two lifetimes: code is executable, recipes are loaded as text into the next
relevant iteration.

**The bus is filesystem + FIFOs, not a server.** Multiple Ralphs share a
directory. Each writes its `<id>.status` file after every iteration; each
reads a named pipe for incoming requests; responses are written as JSON
files. Auto-fulfillment runs on a daemon thread — when a peer's `ask_ralph`
arrives, the bus reads our own filesystem (top-K matches by keyword overlap
on the requester's description) and writes back a response without ever
consulting the LLM. Tool transfers are byte-verbatim because they never go
through the LLM as a string to retype.

**Peer expertise emerges from the prompt.** At startup, one forced-tool LLM
call summarizes the prompt into a short phrase like "Sudoku solving" or
"Caesar-cipher decryption." This is published in the status file. Other
Ralphs see it in their peer preamble and route asks accordingly. No one has
to declare "I am the expert" — the system extracts it from what the task
asks them to do. If no peer specializes in your problem, you solve it
yourself, and the next ralph attacking a similar task will find *you* on
the bus. Find help, or become the help.

**No bash, no shell, no test runner.** Ralph writes Python; verification is
the agent's responsibility. This is a deliberate constraint — it forces
solutions to be correct-by-construction rather than verified-by-bash. (It
also means a `kill -9` mid-iteration leaves a workspace in a consistent
state.)

---

## Related work

**The original Ralph loop.** Ralph is named after Wreck-It Ralph and shaped
by [Geoffrey Huntley's "Ralph Wiggum" technique][rw] — the observation that a
surprising amount of long-horizon work can be done by *just running the same
prompt over and over* until it's done, no clever planner required. This
project keeps that core (re-feed the prompt; the agent rediscovers state from
disk; let iterations carry the load) and adds the surrounding machinery:
dynamic tools, distilled recipes, peer cooperation over a bus, expertise
routing, fail-fast model validation.

[rw]: https://ghuntley.com/ralph/

**Coding agents.** Claude Code, Cursor's agent mode, Aider, OpenHands, and
others all solve the long-horizon coding-task problem with much more
sophisticated UIs and tool-use surfaces. Ralph is intentionally tiny — a few
hundred lines you can read in one sitting. The interesting design choices it
keeps relative to the larger systems: (1) every iteration is a *cold start*
on the LLM side, with the filesystem as the only memory; (2) recipes are
*Python source*, not a vector database; (3) peer cooperation is *file-based*,
so a Ralph crashing or being `kill -9`'d doesn't break anyone else.

**"Deep" / long-horizon agents.** A common pattern in long-horizon agents is
planner → sub-agents → filesystem → growing context. Ralph's loop intentionally
doesn't include an explicit planning phase — the plan, if any, lives in the
prompt file (which the agent re-reads every iteration) and in the accumulated
recipes. Sub-agents are replaced by peer Ralphs on the bus, communicating
asymmetrically (ask + auto-fulfill) rather than via parent → child delegation.

**Recursive Language Models** ([Zhang, Kraska, Khattab, 2025][rlm]). RLMs
treat the LLM input as a variable inside a Python REPL and let the LLM write
code that examines, slices, and recursively self-calls over snippets. The
problem they solve is *input scaling*: prompts up to two orders of magnitude
beyond the context window, in a *single* inference call. Ralph solves the
orthogonal problem of *iteration scaling*: tasks too big for any one
inference, where state must survive across thousands of fresh-context turns.
RLM's substrate is in-memory Python variables scoped to one call; Ralph's
substrate is Python source on disk that lives forever. The two compose
cleanly — Ralph's `config.completion` could in principle be bound to
`rlm.completion` so that within one iteration RLM handles oversized inputs
and across iterations Ralph handles oversized tasks. Both share the
intuition that the LLM should sit above a *programmatic* substrate it
manipulates with code rather than a *string* substrate it reads end to end.

[rlm]: https://arxiv.org/abs/2512.24601

**Agent frameworks.** LangChain / LangGraph, AutoGen, CrewAI, LlamaIndex
Agent, smolagents, and similar libraries provide reusable building blocks
(memory backends, tool wrappers, message routing, planner abstractions).
Ralph is deliberately not a framework — it's a single coherent loop with
opinionated defaults: tools are `.py` files in a specific shape, recipes
are `.py` files in a specific shape, peers are processes pointing at the
same directory, memory selection runs an extra forced-tool LLM call per
iteration. The opinions are what make the codebase fit in your head; if you
need configurability beyond what's there, a framework is the better tool.
