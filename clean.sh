#!/usr/bin/env bash
# Remove demo workspaces (ws_*), the bus, and per-ralph logs.
# WARNING: workspaces are Ralph's filesystem-of-record — wiping them throws away
# all in-progress work. Only run this when you want a clean slate.
set -euo pipefail
cd "$(dirname "$0")"

echo "removing:"
ls -d ws_* bus logs 2>/dev/null || true
rm -rf ws_* bus logs
echo "✅ cleaned"
