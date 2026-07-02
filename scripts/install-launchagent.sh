#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_IN="$ROOT/scripts/com.rena.facebook-watcher.plist.in"
PLIST_DST="${HOME}/Library/LaunchAgents/com.rena.facebook-watcher.plist"
LABEL="com.rena.facebook-watcher"
DOMAIN="gui/$(id -u)"

mkdir -p "$ROOT/logs"
mkdir -p "${HOME}/Library/LaunchAgents"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Creating venv..."
  python3 -m venv "$ROOT/.venv"
fi

echo "Installing Python dependencies..."
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt"

echo "Installing Playwright Chromium into project (PLAYWRIGHT_BROWSERS_PATH)..."
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.playwright-browsers"
"$ROOT/.venv/bin/python" -m playwright install chromium

echo "Writing LaunchAgent plist..."
sed "s|RENA_ROOT|${ROOT}|g" "$PLIST_IN" > "$PLIST_DST"

echo "Stopping existing job (if any)..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true

echo "Loading LaunchAgent..."
if ! launchctl load "$PLIST_DST" 2>/dev/null; then
  echo "(load failed, trying bootstrap)"
  launchctl bootstrap "$DOMAIN" "$PLIST_DST" || true
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
fi
launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true

echo ""
echo "Installed. Label: $LABEL"
echo "Logs: $ROOT/logs/fb-watcher.out.log"
echo "Check: launchctl print \"$DOMAIN/$LABEL\" | head -20"
echo ""
echo "合盖 7x24 提示："
echo "  - 仅插电合盖、无外接屏时，Mac 可能整体睡眠，进程会停。"
echo "  - 建议：系统设置 → 电池 → 选项 → 接通电源时防止自动睡眠；或外接显示器使用合盖模式。"
echo "  - 可选（需管理员，仅在你了解风险时使用）：sudo pmset -c sleep 0"
echo ""
echo "卸载：bash $ROOT/scripts/uninstall-launchagent.sh"
