#!/usr/bin/env bash
# Single-Ralph example — Count letters
# No bus cooperation; just one Ralph wrecking an empty workspace into a working
# implementation. The simplest demo of the iterate-until-done loop.
set -euo pipefail
BUS=./bus_count
source "$(dirname "$0")/_lib.sh"

seed_prompt ws_count <<'EOF'
Compute the answer yourself — you have no shell or exec tool, so do NOT
write Python scripts that you intend to "run later" (you can't run them).

Task: pick 50 fruit names and, for each, count the letter 'R' (case-insensitive).
Write the final mapping to result.json as a flat object {"<fruit>": <r_count>, ...},
EOF

launch count ws_count

echo "🌈 launched 1 ralph (count). Log in ./logs/count.log. Ctrl-C to stop."
wait
