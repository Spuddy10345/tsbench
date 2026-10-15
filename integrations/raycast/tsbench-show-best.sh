#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title tsbench: show best exit nodes
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🛰️
# @raycast.packageName tsbench

# One line from tsbench's history: the top group, the current node and the age of
# the last full run. Instant; nothing is benchmarked or changed.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TSBENCH="${TSBENCH_BIN:-$(command -v tsbench || true)}"
if [ -z "$TSBENCH" ]; then
  echo "tsbench not found. Install it, or set TSBENCH_BIN to its full path."
  exit 1
fi

"$TSBENCH" status --oneline --no-colour
