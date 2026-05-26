#!/usr/bin/env bash
# Parallel list-and-count demo — 4 Ralphs sharing a bus.
#   fruits, animals, states: each generates its own 25-name list and exposes
#     it as a Python tool (workspace/tools/list_*.py).
#   counter: asks each specialist for their list-tool via ask_ralph, then
#     installs the tools, calls them, and tallies 'r' occurrences per name.
#
# Ported from fast-rlm/examples/parallel_r_count.py, which spawns subagents
# in parallel via asyncio.gather. Ralph's analog is the bus: multiple peers
# work concurrently, the aggregator collects via ask_ralph(id, "tools", ...).
set -euo pipefail
BUS=./bus_rcount
source "$(dirname "$0")/_lib.sh"

seed_prompt ws_fruits <<'EOF'
You are the fruits specialist. Build a Python tool that returns a list of
exactly 25 distinct fruit names so other ralphs can fetch it via ask_ralph.

1. Write `tools/list_fruits.py` with:
   - TOOL = {"name": "list_fruits", "description": "Return 25 distinct fruit names as a list of strings.", "input_schema": {"type": "object", "properties": {}}}
   - def run(**kwargs): return a Python list of 25 distinct fruit name strings.
2. Sanity-check the file by reading it back. Confirm exactly 25 names and
   no duplicates.
3. mark_done when the tool is in place.
EOF

seed_prompt ws_animals <<'EOF'
You are the animals specialist. Build a Python tool that returns a list of
exactly 25 distinct animal names so other ralphs can fetch it via ask_ralph.

1. Write `tools/list_animals.py` with:
   - TOOL = {"name": "list_animals", "description": "Return 25 distinct animal names as a list of strings.", "input_schema": {"type": "object", "properties": {}}}
   - def run(**kwargs): return a Python list of 25 distinct animal name strings.
2. Sanity-check by reading the file back. Confirm exactly 25 names and no duplicates.
3. mark_done when the tool is in place.
EOF

seed_prompt ws_states <<'EOF'
You are the US-states specialist. Build a Python tool that returns a list of
exactly 25 distinct US state names so other ralphs can fetch it via ask_ralph.

1. Write `tools/list_states.py` with:
   - TOOL = {"name": "list_states", "description": "Return 25 distinct US state names as a list of strings.", "input_schema": {"type": "object", "properties": {}}}
   - def run(**kwargs): return a Python list of 25 distinct US state name strings.
2. Sanity-check by reading the file back. Confirm exactly 25 names and no duplicates.
3. mark_done when the tool is in place.
EOF

seed_prompt ws_counter <<'EOF'
Build a dictionary mapping each name (string) to the count of the letter 'r'
(case-insensitive) in that name. Pull the three source lists from peers — do
NOT generate them yourself.

Three peers on the bus each expose a list tool:
  - fruits   → list_fruits     (25 fruit names)
  - animals  → list_animals    (25 animal names)
  - states   → list_states     (25 US state names)

Workflow:
1. ask_ralph(id="fruits",  category="tools", description="list of 25 fruit names")
   ask_ralph(id="animals", category="tools", description="list of 25 animal names")
   ask_ralph(id="states",  category="tools", description="list of 25 US state names")
   Each returns a request_id. Save them.
2. Next turn: check_response(request_id) on each. The peer's tool file is
   auto-installed under your own tools/, callable immediately.
3. Call list_fruits(), list_animals(), list_states() to get the three lists.
4. Build a Python dictionary mapping each of the 75 names → number of 'r' /
   'R' characters in that name (case-insensitive count).
5. Save the dict as JSON to solution.json (sorted keys, indent=2).
6. mark_done.
EOF

# Three generators run in parallel. Counter blocks on all three so its first
# ask hits a populated inventory.
launch fruits  ws_fruits
launch animals ws_animals
launch states  ws_states
launch counter ws_counter --wait-for-peer fruits --wait-for-peer animals --wait-for-peer states

echo "🌈 launched 4 ralphs (fruits, animals, states, counter) sharing bus at $BUS"
echo "   counter will wait until all three generators mark done before iterating."
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
