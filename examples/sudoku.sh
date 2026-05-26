#!/usr/bin/env bash
# Sudoku cooperation demo — 2 Ralphs sharing a bus.
#   expert: builds a robust solver tool + recipes.
#   novice: asks the expert for their solver, then solves a puzzle.
#
# Demonstrates: ask_ralph + peer auto-fulfillment, expert/novice routing.
set -euo pipefail
BUS=./bus_sudoku
source "$(dirname "$0")/_lib.sh"

seed_prompt ws_expert <<'EOF'
Solve these sudoku puzzles. Get faster and more reliable as you go.

Puzzle 1:
003020600900305001001806400008102900700000008006708200002609500800203009005010300

Puzzle 2:
200080300060070084030500209000105408000000000402706000301007040720040060004010003

Puzzle 3:
000000907000420180000705026100904000050000040000507009920108000034059000507000000

Save each solution to solution1.txt, solution2.txt, solution3.txt as 81-digit strings.
Mark_done when all three are solved.
EOF

seed_prompt ws_novice <<'EOF'
Solve this sudoku puzzle. Save the solution to solution.txt as an 81-digit string.

500080049000500030067300001150000000000208000000000018700004150030002000490050003
EOF

# Both ralphs launch immediately. The novice's `--wait-for-peer expert` flag
# blocks its iteration loop (inside Ralph itself) until expert publishes
# done=true on the bus, so the novice's first ask is against a fully-built
# expert rather than an empty inventory.
launch expert ws_expert
launch novice ws_novice --wait-for-peer expert

echo "🌈 launched 2 ralphs (expert, novice) sharing bus at $BUS"
echo "   novice will wait until expert marks done before iterating."
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
