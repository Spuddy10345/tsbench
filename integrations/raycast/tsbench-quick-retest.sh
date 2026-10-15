#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title tsbench: quick re-test
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🛰️
# @raycast.packageName tsbench

# Starts `tsbench --quick --apply` in the background and returns at once. It takes
# a few minutes, reroutes traffic while it runs, and switches node only if the
# result is clearly better. Progress goes to ~/.local/state/tsbench/raycast.log.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TSBENCH="${TSBENCH_BIN:-$(command -v tsbench || true)}"
if [ -z "$TSBENCH" ]; then
  echo "tsbench not found. Install it, or set TSBENCH_BIN to its full path."
  exit 1
fi

state="${TSBENCH_HOME:-$HOME/.local/state/tsbench}"
mkdir -p "$state" && chmod 700 "$state"
log="$state/raycast.log"

nohup "$TSBENCH" --quick --apply --quiet --no-colour >>"$log" 2>&1 &
pid=$!

# If it exits straight away (another run holds the lock, Tailscale is down...)
# say why instead of claiming success.
sleep 1
if ! kill -0 "$pid" 2>/dev/null; then
  wait "$pid"
  rc=$?
  tail -n 1 "$log"
  exit "$rc"
fi
echo "Quick re-test started (a few minutes)"
