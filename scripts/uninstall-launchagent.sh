#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_DST="${HOME}/Library/LaunchAgents/com.rena.facebook-watcher.plist"
LABEL="com.rena.facebook-watcher"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
rm -f "$PLIST_DST"
echo "Removed $LABEL (plist deleted if present)."
