#!/usr/bin/env bash
# Caesar-cipher cooperation demo — 2 Ralphs sharing a bus.
#   cryptanalyst: builds encode/decode/brute-force tools + cipher recipes.
#   decoder:      receives a ciphertext with an unknown shift, asks the
#                 cryptanalyst for the brute-force tool, and recovers the message.
#
# Demonstrates: ask_ralph(description=...) picking the right tool out of several.
set -euo pipefail
BUS=./bus_cipher
source "$(dirname "$0")/_lib.sh"

seed_prompt ws_cryptanalyst <<'EOF'
Decrypt these Caesar-encrypted messages. Each was encoded with a different shift.

1. KHOOR ZRUOG
2. MJQQT BTWQI
3. URYYB JBEYQ

Save each plaintext to solution1.txt, solution2.txt, solution3.txt.
EOF

seed_prompt ws_decoder <<'EOF'
Decrypt this Caesar-encrypted message. The shift is unknown (somewhere in 1..25).

WKDQN BRX IRU BRXU KHOS, UDOSK.

Save the plaintext to solution.txt (one line, no quotes).
EOF

# Decoder waits for the cryptanalyst to finish building tools — otherwise its
# first ask_ralph hits an empty inventory and the loop spins on an empty install.
launch cryptanalyst ws_cryptanalyst
launch decoder      ws_decoder --wait-for-peer cryptanalyst

echo "🌈 launched 2 ralphs (cryptanalyst, decoder) sharing bus at $BUS"
echo "   decoder will wait until cryptanalyst marks done before iterating."
echo "   per-ralph logs in ./logs/. Ctrl-C to stop."
wait
