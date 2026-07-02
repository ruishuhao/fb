#!/usr/bin/env bash
# 首次或换账号：有界面登录一次，会话写入 FB_BROWSER_USER_DATA_DIR
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m src.fb_login
