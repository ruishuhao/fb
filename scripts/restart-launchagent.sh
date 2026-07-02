#!/usr/bin/env bash
# 更新 .env（如 FACEBOOK_COOKIE）后执行此脚本，无需重装依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_IN="$ROOT/scripts/com.rena.facebook-watcher.plist.in"
PLIST_DST="${HOME}/Library/LaunchAgents/com.rena.facebook-watcher.plist"
LABEL="com.rena.facebook-watcher"
DOMAIN="gui/$(id -u)"

mkdir -p "$ROOT/logs"
mkdir -p "${HOME}/Library/LaunchAgents"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "缺少虚拟环境，请先运行: bash $ROOT/scripts/install-launchagent.sh"
  exit 1
fi

sed "s|RENA_ROOT|${ROOT}|g" "$PLIST_IN" > "$PLIST_DST"

echo "Stopping $LABEL ..."
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl unload "$PLIST_DST" 2>/dev/null || true

echo "Loading $LABEL ..."
# 用户级 Agent：load 在多数系统上比 bootstrap 更省事
if launchctl load "$PLIST_DST" 2>&1; then
  echo "Loaded OK."
else
  echo "load failed, trying bootstrap ..."
  launchctl bootstrap "$DOMAIN" "$PLIST_DST"
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
fi

launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true

sleep 2
if launchctl print "$DOMAIN/$LABEL" 2>&1 | head -5 | grep -q state; then
  echo ""
  echo "状态:"
  launchctl print "$DOMAIN/$LABEL" 2>&1 | head -12
else
  echo ""
  echo "若未看到服务状态，请把下面完整输出发出来:"
  launchctl print "$DOMAIN/$LABEL" 2>&1 || true
fi

echo ""
echo "日志: tail -f $ROOT/logs/fb-watcher.out.log"
echo "错误: tail -f $ROOT/logs/fb-watcher.err.log"
