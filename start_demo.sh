#!/usr/bin/env bash
# Launch 3 Ralphs (solver, cli, watcher) sharing a bus at ./bus/.
# Output from all three is prefixed by id and streamed to your terminal,
# and tee'd to logs/<id>.log for later inspection.
#
# Ctrl-C stops everything.
#
# To start completely fresh (wipe workspaces too):
#   ./clean.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Fresh bus (it's transient state); keep workspaces so Ralphs resume.
rm -rf bus
mkdir -p ws_expert ws_novice ws_watcher logs

# Seed prompts on first run; preserve any edits on later runs.
[ -f ws_expert/prompt.md ] || cat > ws_expert/prompt.md <<'EOF'
You are a sudoku expert. Build deep, reusable expertise that other ralphs can pull from
the bus. Concretely:

1. Write a robust solver in tools/sudoku_solver.py. The TOOL must accept a 9x9 grid as
   one line of 81 digits (0 = blank) and return the solved grid as a string of 81 digits,
   or an error if unsolvable. Test it against at least three puzzles of varying difficulty
   that you generate yourself.
2. Capture durable lessons in your memory:
   - recipes: the algorithm you used (constraint propagation + backtracking, MRV heuristic,
     etc.), with enough detail that another ralph could re-implement from your recipe alone.
   - worked: specific moves that produced progress (e.g., "tried propagation-only first;
     it solved easy puzzles but stalled on the hard ones, needed to add backtracking").
   - failed: dead ends you hit and how to avoid them.
3. Mark_done only after solver passes all your tests AND your memory has at least one
   lesson in each of {worked, failed, recipes}.
EOF

[ -f ws_novice/prompt.md ] || cat > ws_novice/prompt.md <<'EOF'
Solve this sudoku puzzle:
500080049000500030067300001150000000000208000000000018700004150030002000490050003

Don't reinvent — there's an expert ralph on the bus. Workflow:

1. list_ralphs() — confirm "expert" is present.
2. peek_ralph(id="expert") — if their status shows done=false and they're early in their
   iterations, wait one turn (do something else, like ls/read your workspace) and retry.
3. ask_ralph(id="expert", category="tools") — returns {request_id}. Save the request_id.
4. On a later turn, call check_response(request_id). If status="pending", try again next turn.
   Otherwise the response is {answer: {files: {"sudoku_solver.py": "<source>", ...}}}.
5. Save the expert's tool source to your own tools/<filename>.py via the write tool. After
   write, the loop reloads tools and the expert's solver becomes callable as a regular tool.
6. Optionally also fetch ask_ralph(id="expert", category="recipes") for context.
7. Call the freshly-loaded solver tool on the puzzle above. Save the solution to solution.txt.
8. mark_done.
EOF

[ -f ws_watcher/prompt.md ] || cat > ws_watcher/prompt.md <<'EOF'
Every iteration: call list_ralphs(), then peek_ralph(id) for each ralph other than yourself.
Maintain workspace/dashboard.md with one bullet per ralph: id, iteration, done, last_text.
Continue until all other ralphs report done=true, then mark_done.
EOF

# Kill the whole process group on Ctrl-C / TERM.
# Disable the trap inside itself so `kill 0` (which signals us too) doesn't recurse.
cleanup() {
  trap - INT TERM
  echo
  echo "🛑 stopping all ralphs..."
  kill 0
}
trap cleanup INT TERM

launch() {
  local id="$1" ws="$2"
  (
    # python -u → unbuffered stdout, so output streams in real time through awk/tee.
    "$PY" -u -m ralph \
      --workspace "$ws" \
      --prompt "$ws/prompt.md" \
      --bus-dir ./bus \
      --ralph-id "$id" 2>&1 \
    | awk -v id="$id" '{ print "[" id "] " $0; fflush() }' \
    | tee "logs/$id.log"
  ) &
}

launch expert  ws_expert
launch novice  ws_novice
launch watcher ws_watcher

echo "🌈 launched 3 ralphs (expert, novice, watcher) sharing bus at ./bus/"
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
