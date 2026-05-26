# Shared helpers for Ralph example scripts. Source this near the top of each
# example after setting BUS to the desired bus directory:
#
#   set -euo pipefail
#   BUS=./bus_foo
#   source "$(dirname "$0")/_lib.sh"
#
#   seed_prompt ws_foo <<'EOF'
#   ...
#   EOF
#   launch foo ws_foo
#   wait

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

mkdir -p logs

[ -n "${BUS:-}" ] || { echo "❌ _lib.sh: BUS must be set before sourcing" >&2; exit 1; }
rm -rf "$BUS"

# Kill the whole process group on Ctrl-C / TERM. Disable the trap inside itself
# so `kill 0` (which signals us too) doesn't recurse.
_ralph_cleanup() {
  trap - INT TERM
  echo
  echo "🛑 stopping all ralphs..."
  kill 0
}
trap _ralph_cleanup INT TERM

# seed_prompt <workspace>: read heredoc from stdin and write to <workspace>/prompt.md
# only if it doesn't already exist (preserves user edits on re-runs). Always
# ensures <workspace>/ exists; the heredoc is consumed either way.
seed_prompt() {
  local ws="$1"
  mkdir -p "$ws"
  if [ -f "$ws/prompt.md" ]; then
    cat > /dev/null
  else
    cat > "$ws/prompt.md"
  fi
}

# launch <id> <workspace> [extra ralph args...]
#   Spawns a ralph in the background, prefixes each output line with [id], and
#   tees to logs/<id>.log. python -u keeps stdout unbuffered so the awk filter
#   sees lines in real time.
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
