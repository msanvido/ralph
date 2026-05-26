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
set -euo pipefail
BUS=./bus_multi
source "$(dirname "$0")/_lib.sh"

seed_prompt ws_fib <<'EOF'
For each number in this list, decide whether it is a Fibonacci number.
Save your answers to solution.txt as one line per number: "<n>: yes" or "<n>: no".

Numbers: 13, 21, 32, 55, 64, 89, 100, 144
EOF

seed_prompt ws_prime <<'EOF'
For each number in this list, decide whether it is prime.
Save your answers to solution.txt as one line per number: "<n>: yes" or "<n>: no".

Numbers: 7, 8, 13, 19, 23, 25, 51, 89, 97
EOF

seed_prompt ws_classifier <<'EOF'
Classify the number 89: is it a Fibonacci number, a prime, both, or neither?
Save the answer to solution.txt as a single line: "89: <fibonacci|prime|both|neither>".
EOF

seed_prompt ws_watcher_multi <<'EOF'
Track the progress of every other ralph on the bus. Maintain workspace/dashboard.md
with one line per ralph: id, expertise, iteration, done, last status.
mark_done when every other ralph reports done=true.
EOF

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
