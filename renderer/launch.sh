#!/bin/bash
# Start the wwx.touchbar renderer.
# The Touch Bar has exactly one owner, so the stock t1bridge renderer has to be
# stopped first (it is also disabled, see README.md).
set -u
state="${XDG_STATE_HOME:-$HOME/.local/state}/t1-touchbar"
mkdir -p "$state"
systemctl --user stop t1-touchbar.service >/dev/null 2>&1 || true
sleep 0.2
exec "$HOME/.config/omarchy/plugins/wwx.touchbar/renderer/touchbar.py" \
  >>"$state/ai-stdout.log" 2>&1