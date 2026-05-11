#!/usr/bin/env bash
# Caesar-cipher cooperation demo — 2 Ralphs sharing a bus.
#   cryptanalyst: builds encode/decode/brute-force tools + cipher recipes.
#   decoder:      receives a ciphertext with an unknown shift, asks the
#                 cryptanalyst for the brute-force tool, and recovers the message.
#
# Demonstrates: ask_ralph(description=...) picking the right tool out of several.
# Output is prefixed by id and tee'd to logs/<id>.log. Ctrl-C stops everything.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

BUS=./bus_cipher
rm -rf "$BUS"
mkdir -p ws_cryptanalyst ws_decoder logs

[ -f ws_cryptanalyst/prompt.md ] || cat > ws_cryptanalyst/prompt.md <<'EOF'
You are a Caesar-cipher specialist. Build a reusable toolkit other Ralphs can fetch.

1. tools/caesar_encode.py — TOOL takes {"text": str, "shift": int} and returns
   {"ciphertext": str}. Preserve case; pass non-letters through unchanged.
2. tools/caesar_decode.py — TOOL takes {"text": str, "shift": int} and returns
   {"plaintext": str}. Same case/punctuation rules.
3. tools/caesar_brute_force.py — TOOL takes {"text": str} and tries all 25 non-trivial
   shifts. Score each candidate by how many of the top-20 English words it contains
   (THE, AND, FOR, YOU, ARE, NOT, ...). Return {"best_shift": int, "best_plaintext": str,
   "all_candidates": [{"shift": int, "plaintext": str, "score": int}, ...]}.
4. Capture at least one recipe: the gotchas worth telling another Ralph (e.g., off-by-one
   between A=0 vs A=1, case preservation, scoring heuristic). Recipes are for procedural
   knowledge that doesn't reduce cleanly to code — anything algorithmic belongs in the tools.
5. mark_done after all three tools exist AND you have at least one recipe captured.
EOF

[ -f ws_decoder/prompt.md ] || cat > ws_decoder/prompt.md <<'EOF'
Recover the plaintext of this Caesar-encrypted message. The shift is unknown
(somewhere in 1..25):

WKDQN BRX IRU BRXU KHOS, UDOSK.

There's a Caesar-cipher specialist on the bus. Workflow:

1. Read the "## Peers on the bus" section. Find the peer whose `expertise:` line
   mentions Caesar / cipher.
2. ask_ralph(id=<cryptanalyst_id>, category="tools",
             description="brute force caesar cipher unknown shift") — save the
   request_id. The description should match their brute-force tool, not their
   plain encode/decode tools.
3. On a later turn, check_response(request_id). The answer carries
   {files: {<name>.py: source}}. Save each file to your own tools/<name>.py via
   write — the loop reloads tools automatically.
4. Call the brute-force tool on the ciphertext above. The "best_plaintext" field
   should be the recovered message. Write it to solution.txt (one line, no quotes).
5. mark_done.
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
  (
    "$PY" -u -m ralph \
      --workspace "$ws" \
      --prompt "$ws/prompt.md" \
      --bus-dir "$BUS" \
      --ralph-id "$id" 2>&1 \
    | awk -v id="$id" '{ print "[" id "] " $0; fflush() }' \
    | tee "logs/$id.log"
  ) &
}

launch cryptanalyst ws_cryptanalyst
launch decoder      ws_decoder

echo "🌈 launched 2 ralphs (cryptanalyst, decoder) sharing bus at $BUS"
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
