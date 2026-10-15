#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title tsbench: apply best exit node
# @raycast.mode silent

# Optional parameters:
# @raycast.icon 🛰️
# @raycast.packageName tsbench

# Switches to the best node in tsbench's history. It does not benchmark anything,
# so it returns in a couple of seconds. Raycast shows the last line as a HUD.

# Raycast starts scripts with a minimal PATH.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TSBENCH="${TSBENCH_BIN:-$(command -v tsbench || true)}"
if [ -z "$TSBENCH" ]; then
  echo "tsbench not found. Install it, or set TSBENCH_BIN to its full path."
  exit 1
fi

out=$("$TSBENCH" apply --no-colour 2>&1)
rc=$?
printf '%s\n' "$out" | tail -n 1
exit "$rc"
