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
You are a Fibonacci-numbers specialist. Build a reusable tool other Ralphs can fetch.

1. Write tools/is_fibonacci.py with:
   TOOL = {
     "name": "is_fibonacci",
     "description": "Check whether a non-negative integer is a Fibonacci number",
     "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
   }
   def run(n): -> {"is_fibonacci": bool}
   Use the perfect-square test: n is Fibonacci iff (5n^2 + 4) or (5n^2 - 4) is a perfect square.
   Handle n=0 and n=1 explicitly.
2. Capture at least one recipe — procedural/heuristic knowledge that wouldn't fit cleanly
   in the tool itself (e.g., the perfect-square trick, when O(n) enumeration is fine vs not).
3. mark_done after the tool exists and you have at least one recipe captured.
EOF

[ -f ws_prime/prompt.md ] || cat > ws_prime/prompt.md <<'EOF'
You are a prime-numbers specialist. Build a reusable tool other Ralphs can fetch.

1. Write tools/is_prime.py with:
   TOOL = {
     "name": "is_prime",
     "description": "Check whether a non-negative integer is prime",
     "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
   }
   def run(n): -> {"is_prime": bool}
   Use trial division up to floor(sqrt(n)); handle 0, 1, and 2 correctly.
2. Capture at least one recipe — procedural notes worth telling a future Ralph (edge cases
   like 0/1/2, when trial division is fine vs. when you'd want Miller-Rabin, etc.).
3. mark_done after the tool exists and you have at least one recipe captured.
EOF

[ -f ws_classifier/prompt.md ] || cat > ws_classifier/prompt.md <<'EOF'
Classify the number 89: is it a Fibonacci number, a prime, both, or neither?
Write the answer to solution.txt as a single line: "89: <fibonacci|prime|both|neither>".

Don't reinvent — there are specialists on the bus. Workflow:

1. Read the "## Peers on the bus" section at the top of your prompt. Identify the
   Fibonacci specialist and the prime specialist by their `expertise:` lines.
2. ask_ralph(id=<fib_ralph_id>, category="tools",
             description="check if a number is a Fibonacci") — save the request_id.
3. ask_ralph(id=<prime_ralph_id>, category="tools",
             description="check if a number is prime") — save the request_id.
4. On later turns, check_response for both. The answer carries {files: {<name>.py: source}}.
   Save each tool to your own tools/<name>.py via write — the loop reloads tools
   automatically.
5. Call is_fibonacci(n=89) and is_prime(n=89) as ordinary tools. Combine the booleans
   into the answer ("both", "fibonacci", "prime", or "neither") and write it to
   solution.txt.
6. mark_done.
EOF

[ -f ws_watcher_multi/prompt.md ] || cat > ws_watcher_multi/prompt.md <<'EOF'
Every iteration: call list_ralphs(), then peek_ralph(id) for each ralph other than yourself.
Maintain workspace/dashboard.md with one bullet per ralph: id, expertise, iteration, done,
last_text. mark_done when every other ralph reports done=true.
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
