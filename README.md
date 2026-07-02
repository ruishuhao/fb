# Facebook Post Watcher (Alert-Only)

This project monitors a target public Facebook URL and alerts you when new posts appear.

## Important

- This tool is **alert-only**. It does **not** auto-reply or auto-post.
- Auto-reply bots can violate platform rules and may risk account restrictions.
- Use only for public content you are allowed to monitor.

## Features

- Poll target page at a configurable interval
- Detect new post links from page HTML
- Desktop notification + terminal alert
- Optional auto-open browser when new post is found
- Local JSON state to avoid duplicate alerts

## Quick Start

1. Create and activate a virtual env:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Edit `.env` values.

4. Run:

```bash
python -m src.main
```

### Long-lived Facebook session (no cookie string)

1. In `.env` set `FB_BROWSER_USER_DATA_DIR=.fb-browser-profile` (or any path under the project).
2. One-time login (saves cookies/local storage into that folder):

```bash
bash scripts/fb-browser-login.sh
```

3. Run the watcher as usual (`python -m src.main` or your LaunchAgent). You can leave `FACEBOOK_COOKIE` empty.
4. If Facebook asks for login again later, repeat step 2.

## Configuration

| Key | Description | Default |
| --- | --- | --- |
| `TARGET_URL` | Public Facebook URL to monitor | (required) |
| `CHECK_INTERVAL_SECONDS` | Poll interval seconds | `20` |
| `MATCH_KEYWORDS` | Optional comma-separated keywords | empty |
| `OPEN_ON_ALERT` | Open post in browser on alert (`true/false`) | `false` |
| `REQUEST_TIMEOUT_SECONDS` | HTTP timeout seconds | `10` |
| `STATE_FILE` | Local state path | `.watcher_state.json` |
| `FACEBOOK_COOKIE` | Optional Facebook session cookie | empty |
| `FB_BROWSER_USER_DATA_DIR` | Persistent Chromium profile (same session as manual login); set to e.g. `.fb-browser-profile` to avoid pasting cookies | empty |
| `FB_BROWSER_HEADLESS` | `false` / `0` / `off` = show browser (for debugging); default headless | `true` |
| `FB_ACCEPT_LANGUAGE` | Accept-Language header | `zh-CN,zh;q=0.9,en;q=0.8` |
| `WECOM_WEBHOOK_URL` | WeCom bot webhook URL | empty |
| `SERVERCHAN_SENDKEY` | ServerChan key for WeChat push | empty |
| `COOKIE_ALERT_SENDKEY` | Optional extra ServerChan key only for “session dead” alerts; if empty, those alerts use `SERVERCHAN_SENDKEY` / `WECOM_WEBHOOK_URL` | empty |
| `COOKIE_ALERT_CONSECUTIVE_FAILS` | Consecutive all-target failure rounds before session alert | `4` |

## 7×24 on macOS

- 使用 `bash scripts/install-launchagent.sh` 安装后，`com.rena.facebook-watcher` 会 **RunAtLoad + KeepAlive** 常驻；更新代码或 `.env` 后执行 `bash scripts/restart-launchagent.sh`。
- 合盖仅插电时若系统整体睡眠，进程仍会停，请在 **系统设置 → 电池** 中允许接通电源时防止睡眠，或外接显示器合盖模式。

## Notes

- For better stability, use publicly accessible pages.
- If Facebook page markup changes, update URL extraction rules in `src/fb_monitor.py`.
- Full config file is `.env`.

## WeChat Push Tips

- **WeCom bot**: add a group bot in WeCom, copy webhook URL to `WECOM_WEBHOOK_URL`.
- **ServerChan**: register and create `SendKey`, then fill `SERVERCHAN_SENDKEY`.
- You can enable both channels at the same time.
- When Facebook login looks broken (all targets empty/failing), an alert is sent on the same WeCom / ServerChan / SMTP channels as new-post notifications (no need for a separate key unless you set `COOKIE_ALERT_SENDKEY`).
