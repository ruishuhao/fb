from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


@dataclass(frozen=True)
class AppConfig:
    target_url: str
    extra_target_urls: list[str]
    check_interval_seconds: int
    match_keywords: list[str]
    open_on_alert: bool
    request_timeout_seconds: int
    state_file: str
    facebook_cookie: str
    fb_browser_user_data_dir: str
    fb_browser_headless: bool
    fb_accept_language: str
    fb_email: str
    fb_password: str
    wecom_webhook_url: str
    serverchan_sendkey: str
    notify_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    email_to: str
    telegram_bot_token: str
    telegram_chat_ids: list[str]
    cookie_alert_sendkey: str
    cookie_alert_consecutive_fails: int
    fetch_error_auto_retry: bool
    fetch_recovery_sleep_seconds: int
    extra_new_post_max_age_seconds: int
    extra_feed_visual_enabled: bool
    extra_feed_visual_notify: bool
    extra_feed_visual_clip_height: int
    extra_feed_visual_min_hamming: int
    extra_feed_ocr_enabled: bool
    extra_feed_ocr_lang: str
    extra_feed_ocr_notify: bool
    extra_profile_feed_scroll_passes: int


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_under_project(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    path = Path(raw)
    if not path.is_absolute():
        path = _project_root() / path
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def load_config() -> AppConfig:
    env_path = _project_root() / ".env"
    # Prefer local .env values over inherited shell env vars.
    load_dotenv(dotenv_path=env_path, override=True)
    env_file_values = dotenv_values(dotenv_path=env_path)

    # Explicitly honor .env for monitor target toggles.
    target_url = str(env_file_values.get("TARGET_URL") or "").strip()
    raw_extra_urls = str(env_file_values.get("EXTRA_TARGET_URLS") or "").strip()
    extra_target_urls = [u.strip() for u in raw_extra_urls.split(",") if u.strip()]
    if not target_url and not extra_target_urls:
        raise ValueError("Missing monitor targets in .env (set TARGET_URL or EXTRA_TARGET_URLS)")

    check_interval_seconds = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))
    request_timeout_seconds = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
    open_on_alert = _parse_bool(os.getenv("OPEN_ON_ALERT"), default=False)
    state_file = os.getenv("STATE_FILE", ".watcher_state.json").strip()
    facebook_cookie = os.getenv("FACEBOOK_COOKIE", "").strip()
    fb_browser_user_data_dir = _resolve_under_project(
        str(env_file_values.get("FB_BROWSER_USER_DATA_DIR") or "").strip()
    )
    _raw_bh = str(
        env_file_values.get("FB_BROWSER_HEADLESS") or os.getenv("FB_BROWSER_HEADLESS") or ""
    ).strip().lower()
    fb_browser_headless = _raw_bh not in {"0", "false", "no", "off", "n"}
    fb_accept_language = os.getenv("FB_ACCEPT_LANGUAGE", "zh-CN,zh;q=0.9,en;q=0.8").strip()
    fb_email = os.getenv("FB_EMAIL", "").strip()
    fb_password = os.getenv("FB_PASSWORD", "").strip()
    wecom_webhook_url = os.getenv("WECOM_WEBHOOK_URL", "").strip()
    serverchan_sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
    notify_enabled = _parse_bool(os.getenv("NOTIFY_ENABLED"), default=True)
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    email_to = os.getenv("EMAIL_TO", "").strip()
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_tg_ids = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    telegram_chat_ids = [c.strip() for c in raw_tg_ids.split(",") if c.strip()]
    cookie_alert_sendkey = os.getenv("COOKIE_ALERT_SENDKEY", "").strip()
    cookie_alert_consecutive_fails = int(os.getenv("COOKIE_ALERT_CONSECUTIVE_FAILS", "3"))
    fetch_error_auto_retry = _parse_bool(os.getenv("FETCH_ERROR_AUTO_RETRY"), default=True)
    fetch_recovery_sleep_seconds = int(os.getenv("FETCH_RECOVERY_SLEEP_SECONDS", "4"))

    _eh = str(
        env_file_values.get("EXTRA_NEW_POST_MAX_AGE_HOURS")
        or os.getenv("EXTRA_NEW_POST_MAX_AGE_HOURS")
        or "48"
    ).strip()
    try:
        extra_hours = float(_eh)
    except ValueError:
        extra_hours = 48.0
    extra_hours = max(1.0, min(168.0, extra_hours))
    extra_new_post_max_age_seconds = int(extra_hours * 3600)

    extra_feed_visual_enabled = _parse_bool(
        str(env_file_values.get("EXTRA_FEED_VISUAL_ENABLED") or os.getenv("EXTRA_FEED_VISUAL_ENABLED") or ""),
        default=False,
    )
    extra_feed_visual_notify = _parse_bool(
        str(env_file_values.get("EXTRA_FEED_VISUAL_NOTIFY") or os.getenv("EXTRA_FEED_VISUAL_NOTIFY") or ""),
        default=False,
    )
    try:
        extra_feed_visual_clip_height = int(
            str(env_file_values.get("EXTRA_FEED_VISUAL_CLIP_HEIGHT") or os.getenv("EXTRA_FEED_VISUAL_CLIP_HEIGHT") or "900").strip()
        )
    except ValueError:
        extra_feed_visual_clip_height = 900
    extra_feed_visual_clip_height = max(400, min(1600, extra_feed_visual_clip_height))
    try:
        extra_feed_visual_min_hamming = int(
            str(env_file_values.get("EXTRA_FEED_VISUAL_MIN_HAMMING") or os.getenv("EXTRA_FEED_VISUAL_MIN_HAMMING") or "14").strip()
        )
    except ValueError:
        extra_feed_visual_min_hamming = 14
    extra_feed_visual_min_hamming = max(4, min(32, extra_feed_visual_min_hamming))

    extra_feed_ocr_enabled = _parse_bool(
        str(env_file_values.get("EXTRA_FEED_OCR_ENABLED") or os.getenv("EXTRA_FEED_OCR_ENABLED") or ""),
        default=False,
    )
    extra_feed_ocr_lang = str(
        env_file_values.get("EXTRA_FEED_OCR_LANG") or os.getenv("EXTRA_FEED_OCR_LANG") or "eng+chi_sim"
    ).strip() or "eng+chi_sim"
    extra_feed_ocr_notify = _parse_bool(
        str(env_file_values.get("EXTRA_FEED_OCR_NOTIFY") or os.getenv("EXTRA_FEED_OCR_NOTIFY") or ""),
        default=False,
    )

    # Extra 主页：时间线向下滚动的采样次数（0=只采当前视口/首屏，符合「不滚动只认首条」+ 视口 OCR 主流程；>0 为爬虫兜底多抓链接）
    try:
        extra_profile_feed_scroll_passes = int(
            str(
                env_file_values.get("EXTRA_PROFILE_FEED_SCROLL_PASSES")
                or os.getenv("EXTRA_PROFILE_FEED_SCROLL_PASSES")
                or "0"
            ).strip()
        )
    except ValueError:
        extra_profile_feed_scroll_passes = 0
    extra_profile_feed_scroll_passes = max(0, min(20, extra_profile_feed_scroll_passes))

    raw_keywords = os.getenv("MATCH_KEYWORDS", "").strip()
    match_keywords = [k.strip().lower() for k in raw_keywords.split(",") if k.strip()]

    return AppConfig(
        target_url=target_url,
        extra_target_urls=extra_target_urls,
        check_interval_seconds=max(5, check_interval_seconds),
        match_keywords=match_keywords,
        open_on_alert=open_on_alert,
        request_timeout_seconds=max(3, request_timeout_seconds),
        state_file=state_file,
        facebook_cookie=facebook_cookie,
        fb_browser_user_data_dir=fb_browser_user_data_dir,
        fb_browser_headless=fb_browser_headless,
        fb_accept_language=fb_accept_language,
        fb_email=fb_email,
        fb_password=fb_password,
        wecom_webhook_url=wecom_webhook_url,
        serverchan_sendkey=serverchan_sendkey,
        notify_enabled=notify_enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_from=smtp_from,
        email_to=email_to,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_ids=telegram_chat_ids,
        cookie_alert_sendkey=cookie_alert_sendkey,
        cookie_alert_consecutive_fails=max(1, cookie_alert_consecutive_fails),
        fetch_error_auto_retry=fetch_error_auto_retry,
        fetch_recovery_sleep_seconds=max(1, min(120, fetch_recovery_sleep_seconds)),
        extra_new_post_max_age_seconds=max(3600, extra_new_post_max_age_seconds),
        extra_feed_visual_enabled=extra_feed_visual_enabled,
        extra_feed_visual_notify=extra_feed_visual_notify,
        extra_feed_visual_clip_height=extra_feed_visual_clip_height,
        extra_feed_visual_min_hamming=extra_feed_visual_min_hamming,
        extra_feed_ocr_enabled=extra_feed_ocr_enabled,
        extra_feed_ocr_lang=extra_feed_ocr_lang,
        extra_feed_ocr_notify=extra_feed_ocr_notify,
        extra_profile_feed_scroll_passes=extra_profile_feed_scroll_passes,
    )
