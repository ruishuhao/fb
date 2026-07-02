from __future__ import annotations

import smtplib
import subprocess
import sys
import webbrowser
from datetime import datetime
from email.message import EmailMessage

import requests
try:
    from plyer import notification
except Exception:  # pragma: no cover
    notification = None


def _desktop_notify(title: str, message: str) -> None:
    # macOS 上 plyer 依赖 pyobjus（未安装时静默失败），改走 osascript
    if sys.platform == "darwin":
        script = 'display notification "{}" with title "{}"'.format(
            message.replace("\\", "\\\\").replace('"', '\\"')[:200],
            title.replace("\\", "\\\\").replace('"', '\\"')[:100],
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
        )
        return
    if notification is not None:
        notification.notify(title=title, message=message, timeout=5)


def _push_wecom(webhook_url: str, title: str, message: str, url: str) -> None:
    if not webhook_url:
        return
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": f"**{title}**\n\n{message}\n\n[点击打开帖子]({url})",
        },
    }
    resp = requests.post(webhook_url, json=payload, timeout=8)
    resp.raise_for_status()


def _push_serverchan(sendkey: str, title: str, message: str, url: str) -> None:
    if not sendkey:
        return
    api = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {
        "title": title,
        "desp": f"{message}\n\n[点击打开帖子]({url})",
    }
    resp = requests.post(api, data=payload, timeout=8)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"serverchan invalid response: {exc}") from exc
    if data.get("code") != 0:
        raise RuntimeError(f"serverchan error: {data}")


def _push_email(
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_from: str,
    email_to: str,
    title: str,
    message: str,
    url: str,
) -> None:
    if not (smtp_host and smtp_port and smtp_username and smtp_password and smtp_from and email_to):
        return
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = smtp_from
    msg["To"] = email_to
    msg.set_content(f"{message}\n\n打开链接: {url}")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


def _push_telegram(bot_token: str, chat_ids: list[str], title: str, message: str, url: str) -> None:
    if not bot_token or not chat_ids:
        return
    text = f"*{title}*\n\n{message}"
    if url:
        text += f"\n\n[打开链接]({url})"
    api = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        resp = requests.post(api, json=payload, timeout=10)
        resp.raise_for_status()


def notify_new_post(
    title: str,
    message: str,
    url: str,
    open_in_browser: bool = False,
    wecom_webhook_url: str = "",
    serverchan_sendkey: str = "",
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_username: str = "",
    smtp_password: str = "",
    smtp_from: str = "",
    email_to: str = "",
    telegram_bot_token: str = "",
    telegram_chat_ids: list[str] | None = None,
) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {title} - {message}")
    print(f"URL: {url}")

    try:
        _desktop_notify(title, message)
    except Exception:
        # Desktop notifications may fail on some environments.
        pass

    try:
        _push_wecom(wecom_webhook_url, title, message, url)
    except Exception as exc:
        print(f"[{now}] wecom push failed: {exc}")

    sendkeys = [k.strip() for k in serverchan_sendkey.replace("，", ",").split(",") if k.strip()]
    if not sendkeys and serverchan_sendkey.strip():
        sendkeys = [serverchan_sendkey.strip()]
    for sendkey in sendkeys:
        try:
            _push_serverchan(sendkey, title, message, url)
        except Exception as exc:
            print(f"[{now}] serverchan push failed ({sendkey[:8]}...): {exc}")

    try:
        _push_email(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=smtp_username,
            smtp_password=smtp_password,
            smtp_from=smtp_from,
            email_to=email_to,
            title=title,
            message=message,
            url=url,
        )
    except Exception as exc:
        print(f"[{now}] email push failed: {exc}")

    try:
        _push_telegram(telegram_bot_token, telegram_chat_ids or [], title, message, url)
    except Exception as exc:
        print(f"[{now}] telegram push failed: {exc}")

    if open_in_browser:
        webbrowser.open(url)
