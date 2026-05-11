#!/usr/bin/env bash
# Multi-expert cooperation demo — shows off the expertise-routed protocol.
#   fib:        Fibonacci-numbers specialist (builds is_fibonacci tool).
#   prime:      Prime-numbers specialist (builds is_prime tool).
#   classifier: novice that must classify a number — has to ROUTE asks to the
#               right specialist based on each peer's published `expertise`.
#   watcher:    maintains a dashboard.
#
# Demonstrates: ask_ralph(..., description=...), top-3 ranking on the responder,
# and expertise-based routing surfaced in the peer preamble.
# Ctrl-C stops everything. Per-ralph logs in ./logs/.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

BUS=./bus_multi
rm -rf "$BUS"
mkdir -p ws_fib ws_prime ws_classifier ws_watcher_multi logs

[ -f ws_fib/prompt.md ] || cat > ws_fib/prompt.md <<'EOF'
For each number in this list, decide whether it is a Fibonacci number.
Save your answers to solution.txt as one line per number: "<n>: yes" or "<n>: no".

Numbers: 13, 21, 32, 55, 64, 89, 100, 144
EOF

[ -f ws_prime/prompt.md ] || cat > ws_prime/prompt.md <<'EOF'
For each number in this list, decide whether it is prime.
Save your answers to solution.txt as one line per number: "<n>: yes" or "<n>: no".

Numbers: 7, 8, 13, 19, 23, 25, 51, 89, 97
EOF

[ -f ws_classifier/prompt.md ] || cat > ws_classifier/prompt.md <<'EOF'
Classify the number 89: is it a Fibonacci number, a prime, both, or neither?
Save the answer to solution.txt as a single line: "89: <fibonacci|prime|both|neither>".
EOF

[ -f ws_watcher_multi/prompt.md ] || cat > ws_watcher_multi/prompt.md <<'EOF'
Track the progress of every other ralph on the bus. Maintain workspace/dashboard.md
with one line per ralph: id, expertise, iteration, done, last status.
mark_done when every other ralph reports done=true.
EOF

cleanup() {
  trap - INT TERM
  echo
  echo "🛑 stopping all ralphs..."
  kill 0
}
trap cleanup INT TERM

launch() {
  local id="$1" ws="$2"
  shift 2
  (
    "$PY" -u -m ralph \
      --workspace "$ws" \
      --prompt "$ws/prompt.md" \
      --bus-dir "$BUS" \
      --ralph-id "$id" \
      "$@" 2>&1 \
    | awk -v id="$id" '{ print "[" id "] " $0; fflush() }' \
    | tee "logs/$id.log"
  ) &
}

# Classifier waits for BOTH specialists to finish before asking. The watcher
# runs in parallel — its whole purpose is to observe the others mid-flight.
launch fib        ws_fib
launch prime      ws_prime
launch classifier ws_classifier --wait-for-peer fib --wait-for-peer prime
launch watcher    ws_watcher_multi

echo "🌈 launched 4 ralphs (fib, prime, classifier, watcher) sharing bus at $BUS"
echo "   classifier will wait until fib AND prime mark done before iterating."
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
