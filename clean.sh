#!/usr/bin/env bash
# Remove every artifact the examples in examples/ create:
#   - per-example workspaces (ws_solo, ws_expert, ws_novice, ws_cryptanalyst,
#     ws_decoder, ws_fib, ws_prime, ws_classifier, ws_watcher_multi,
#     ws_fruits, ws_animals, ws_states, ws_counter, ws_podcast)
#   - per-example bus dirs (bus_solo, bus_sudoku, bus_cipher, bus_multi,
#     bus_rcount, bus_podcast)
#   - the legacy ./bus dir from the manual walkthrough in README.md
#   - the shared ./logs/ directory
#
# WARNING: workspaces are Ralph's filesystem-of-record — wiping them throws away
# all in-progress work. Only run this when you want a clean slate.
set -euo pipefail
shopt -s nullglob
cd "$(dirname "$0")"

paths=(ws_* bus bus_* logs)
matches=()
for p in "${paths[@]}"; do
  [ -e "$p" ] && matches+=("$p")
done

if [ ${#matches[@]} -eq 0 ]; then
  echo "nothing to clean."
  exit 0
fi

echo "removing:"
printf '  %s\n' "${matches[@]}"
rm -rf "${matches[@]}"
echo "✅ cleaned"
