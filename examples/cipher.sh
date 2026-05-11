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
Decrypt these Caesar-encrypted messages. Each was encoded with a different shift.

1. KHOOR ZRUOG
2. MJQQT BTWQI
3. URYYB JBEYQ

Save each plaintext to solution1.txt, solution2.txt, solution3.txt.
EOF

[ -f ws_decoder/prompt.md ] || cat > ws_decoder/prompt.md <<'EOF'
Decrypt this Caesar-encrypted message. The shift is unknown (somewhere in 1..25).

WKDQN BRX IRU BRXU KHOS, UDOSK.

Save the plaintext to solution.txt (one line, no quotes).
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

# Decoder waits for the cryptanalyst to finish building tools — otherwise its
# first ask_ralph hits an empty inventory and the loop spins on an empty install.
launch cryptanalyst ws_cryptanalyst
launch decoder      ws_decoder --wait-for-peer cryptanalyst

echo "🌈 launched 2 ralphs (cryptanalyst, decoder) sharing bus at $BUS"
echo "   decoder will wait until cryptanalyst marks done before iterating."
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
