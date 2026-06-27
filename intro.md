# Ralph

**Ralph** is a minimal agent harness built around one idea: agents should behave the way humans do when they're productive — they build tools, and they share them.

Inspired by [Ralph Wiggum loops](https://ghuntley.com/ralph/), [Recursive Language Models](https://arxiv.org/abs/2512.24601), CSP (Hoare processes), code-as-agents, and Conway's Game of Life — but stripped down to two primitives:

1. **A self-improving Python program.** Python is the agent. No sandbox, no scaffolding. When the agent needs capability, it defines a Python function and can call it immediately on the next turn — the harness reloads `workspace/tools/` after every write.

2. **Agents share what they build.** Before writing a tool, a Ralph asks its peers. If one already has it, the source transfers verbatim — no LLM round-trip on the responder side. Tools accumulate across agents the way techniques spread through a team.

That's the whole system. State, tools, and memory are `.py` files on disk. The bus is a directory. Sharing is a file copy.
