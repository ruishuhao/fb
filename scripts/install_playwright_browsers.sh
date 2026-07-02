#!/usr/bin/env bash
# Playwright 无头模式需要 chromium-headless-shell，仅 chromium 会启动失败。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "未找到 $PY，请先创建 .venv" >&2
  exit 1
fi
exec "$PY" -m playwright install chromium chromium-headless-shell
