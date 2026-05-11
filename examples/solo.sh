#!/usr/bin/env bash
# Single-Ralph example — Conway's Game of Life.
# No bus cooperation; just one Ralph wrecking an empty workspace into a working
# implementation. The simplest demo of the iterate-until-done loop.
#
# Ctrl-C stops it. Workspace persists in ./ws_solo/, log in ./logs/solo.log.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

BUS=./bus_solo
rm -rf "$BUS"
mkdir -p ws_solo logs

[ -f ws_solo/prompt.md ] || cat > ws_solo/prompt.md <<'EOF'
Implement Conway's Game of Life in pure Python.

1. Write life.py with a `Grid` class supporting:
   - Grid.from_string(text) — parse a grid where '#' is alive and '.' is dead;
     rows are separated by newlines.
   - grid.step() — return a new Grid representing the next generation.
   - grid.alive_count — int, count of live cells.
   - grid.to_string() — round-trip back to the same '.'/'#' format.
2. Write test_life.py with tests for the three classic patterns:
   - block (still life — does not change),
   - blinker (oscillator with period 2),
   - glider (after 4 steps, has translated diagonally).
3. Read your tests carefully and reason about whether they pass. You don't have
   a shell — verification is by code review, not by running tests.
4. mark_done when life.py and test_life.py both exist and you're confident the
   logic is correct.
EOF

"$PY" -u -m ralph \
  --workspace ws_solo \
  --prompt ws_solo/prompt.md \
  --bus-dir "$BUS" \
  --ralph-id solo 2>&1 \
  | tee logs/solo.log
