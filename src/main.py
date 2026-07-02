from __future__ import annotations

import atexit
import hashlib
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from src.config import AppConfig, load_config
from src.fb_monitor import FacebookPostMonitor, PostItem
from src.notifier import notify_new_post
from src.state_store import StateStore

# Keep stdout file object alive after dup2 so it is not garbage-collected.
_LOG_STDOUT_HOLDER: list[object] = []
_SESSION_ALERT_NO_CHANNEL_LOGGED = False

_PID_FILE = Path(__file__).resolve().parent.parent / "logs" / "fb-watcher.pid"
# 所有活跃的 Monitor 实例，atexit / SIGTERM 时统一关闭浏览器
_active_monitors: list = []


def _cleanup_monitors() -> None:
    for m in list(_active_monitors):
        try:
            m.close()
        except Exception:
            pass
    _active_monitors.clear()


def _release_pid_lock() -> None:
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _handle_sigterm(_signum, _frame) -> None:
    _cleanup_monitors()
    _release_pid_lock()
    sys.exit(0)


def _acquire_pid_lock() -> None:
    """启动时检测并终止旧实例，确保同时只有一个监控进程运行。"""
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    old_pid: int | None = None
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
        except (ValueError, OSError):
            old_pid = None
    if old_pid and old_pid != os.getpid():
        try:
            os.kill(old_pid, 0)  # 检查进程是否存活
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] startup: 检测到旧进程 PID {old_pid} 仍在运行，正在终止旧实例...", flush=True)
            # 先杀子进程（Playwright node + Chrome），再杀主进程
            try:
                subprocess.run(["pkill", "-P", str(old_pid)], capture_output=True, timeout=5)
            except Exception:
                pass
            time.sleep(1)
            try:
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(2)
                try:
                    os.kill(old_pid, 0)
                    os.kill(old_pid, signal.SIGKILL)  # 还活着则强制杀
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            print(f"[{ts}] startup: 旧进程 PID {old_pid} 已终止", flush=True)
        except ProcessLookupError:
            pass  # 进程已不存在，正常覆盖 PID 文件即可
    _PID_FILE.write_text(str(os.getpid()))
    atexit.register(_cleanup_monitors)
    atexit.register(_release_pid_lock)
    signal.signal(signal.SIGTERM, _handle_sigterm)


def _maybe_truncate_watcher_out_log() -> None:
    """When logs/fb-watcher.out.log has ≥ N lines, truncate to avoid huge files (LaunchAgent stdout)."""
    if sys.stdout.isatty():
        return
    try:
        cap = int(os.getenv("LOG_MAX_LINES_BEFORE_TRUNCATE", "5000"))
    except ValueError:
        cap = 5000
    cap = max(100, min(cap, 500_000))
    root = Path(__file__).resolve().parent.parent
    log_path = root / "logs" / "fb-watcher.out.log"
    if not log_path.is_file():
        return
    try:
        n = 0
        with log_path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                n += chunk.count(b"\n")
                if n >= cap:
                    break
        if n < cap:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[{ts}] log auto-truncated (≥{cap} lines)\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_path, "w", encoding="utf-8", buffering=1)
        log_f.write(msg)
        log_f.flush()
        os.dup2(log_f.fileno(), 1)
        sys.stdout = log_f
        _LOG_STDOUT_HOLDER.clear()
        _LOG_STDOUT_HOLDER.append(log_f)
    except OSError:
        pass


def _recover_browser_monitor(m: FacebookPostMonitor | None, label: str) -> None:
    """Close Playwright page/context so the next fetch builds a fresh browser session."""
    if m is None or not m.use_browser_mode:
        return
    try:
        m.close()
    except Exception:
        pass
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {label}: auto-recovery — browser session closed, will reconnect on next fetch")


def _latest_story_png_path() -> Path:
    return Path(__file__).resolve().parent.parent / "logs" / "fb_latest_post_story.png"


def _screenshot_age_seconds() -> int | None:
    """logs/fb_latest_post_story.png 距 now 的秒数；不存在返回 None。"""
    p = _latest_story_png_path()
    try:
        st = p.stat()
    except OSError:
        return None
    return max(0, int(time.time() - st.st_mtime))


def _screenshot_watchdog_check(
    extra_sources: list[dict],
    cfg: AppConfig,
    last_force_recover_ts: float,
) -> float:
    """如果首条 story 截图超过 `SCREENSHOT_STALE_SECONDS`（默认 600s）没更新：
    认为 watcher 已无效，把所有 extra 源的浏览器整体关闭，让下一轮强制重建 + 重登。
    返回新的 `last_force_recover_ts`，用于内部节流（每 5 分钟最多重建 1 次）。
    """
    stale_threshold = max(60, int(getattr(cfg, "screenshot_stale_seconds", 150) or 150))
    cooldown = 300  # 5 分钟内不重复触发整体重建
    age = _screenshot_age_seconds()
    if age is None:
        return last_force_recover_ts
    if age < stale_threshold:
        return last_force_recover_ts
    now = time.time()
    if now - last_force_recover_ts < cooldown:
        return last_force_recover_ts
    ts = datetime.now().strftime("%H:%M:%S")
    print(
        f"[{ts}] watchdog: fb_latest_post_story.png 已 {age}s 未更新（阈值 {stale_threshold}s），"
        f"强制重建所有 extra 浏览器会话",
        flush=True,
    )
    for source in extra_sources:
        _recover_browser_monitor(source["monitor"], source["label"])
        source["ocr_fail_streak"] = 0
    return now


def _can_push_session_alert(cfg: AppConfig) -> bool:
    """WeChat / mail / Telegram channels that can notify when Facebook session looks dead."""
    if cfg.wecom_webhook_url.strip():
        return True
    if (cfg.cookie_alert_sendkey or cfg.serverchan_sendkey).strip():
        return True
    if cfg.telegram_bot_token.strip() and cfg.telegram_chat_ids:
        return True
    return bool(
        cfg.smtp_host.strip()
        and cfg.email_to.strip()
        and cfg.smtp_from.strip()
        and cfg.smtp_username.strip()
        and cfg.smtp_password.strip()
    )


def _match_keywords(post: PostItem, keywords: list[str]) -> bool:
    if not keywords:
        return True
    target = f"{post.text} {post.url}".lower()
    return any(keyword in target for keyword in keywords)


def _post_signature(post: PostItem) -> str:
    detail = re.sub(r"\s+", " ", post.detail or "").strip().lower()
    detail = re.sub(
        r"\blisted\s+(?:just now|a minute ago|an hour ago|\d+\s+(?:minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago)\b",
        "",
        detail,
    )
    detail = re.sub(r"location is approximate", "", detail)
    detail = re.sub(r"seller information|seller details", "", detail)
    detail = re.sub(r"\blike\b|\bshare\b|\bedit\b|\bdetails\b|\bmark as sold\b|\bmark as pending\b", "", detail)
    detail = re.sub(r"\b\d{10,}\b", "", detail)
    detail = re.sub(r"\s+", " ", detail).strip()
    text = re.sub(r"(?i)just listed", "", post.text or "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    basis = f"{post.url}|{detail or text}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _item_name(post: PostItem) -> str:
    if post.detail:
        title_part = post.detail.split("|", 1)[0].strip()
        if title_part:
            return title_part[:80]
    raw = (post.text or "").strip()
    if raw and raw != "Marketplace listing":
        compact = re.sub(r"\s+", " ", raw)
        return compact[:80]
    match = re.search(r"/marketplace/item/(\d+)", post.url)
    if match:
        return f"item/{match.group(1)}"
    return "未知商品"


def _item_code_from_url(url: str) -> str:
    match = re.search(r"/marketplace/item/(\d+)", url)
    if match:
        return match.group(1)
    return "unknown"


def _is_valid_changed_item(post: PostItem) -> bool:
    name = _item_name(post).strip().lower()
    if not name:
        return False
    if name in {"notifications", "marketplace"}:
        return False
    if len(name) < 3:
        return False
    return True


def _is_pending_review_item(post: PostItem) -> bool:
    content = f"{post.text} {post.detail}".lower()
    return bool(re.search(r"审核中|審核中|pending|in review|under review", content))


def _change_event_key(post: PostItem) -> str:
    detail = (post.detail or "").lower()
    detail = re.sub(r"\s+", " ", detail).strip()
    # Remove time-like volatile phrases.
    detail = re.sub(
        r"\blisted\s+(?:just now|a minute ago|an hour ago|\d+\s+(?:minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago)\b",
        "",
        detail,
    )
    detail = re.sub(r"\b\d+\s*(?:分钟前|小时前|天前|周前|个月前)\b", "", detail)
    # Remove common UI/action words that fluctuate.
    detail = re.sub(
        r"\b(like|share|edit|details|condition|seller information|seller details|location is approximate|mark as sold|mark as pending)\b",
        "",
        detail,
    )
    detail = re.sub(r"\s+", " ", detail).strip()
    base = f"{post.url}|{detail or _item_name(post).lower()}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def _extra_state_path(base_state_file: str, index: int) -> str:
    p = Path(base_state_file)
    return str(p.with_name(f"{p.stem}_extra_{index}{p.suffix}"))


def _extra_story_display_body(top: PostItem, monitor: FacebookPostMonitor | None) -> str:
    """首条帖展示：与抓取一致，**story 截图 OCR 优先**，再帖子的 text/detail（DOM/爬虫兜底）。"""
    if monitor:
        raw = (getattr(monitor, "last_top_post_story_ocr_text", "") or "").strip()
        if raw:
            ex = FacebookPostMonitor._extract_post_body_from_ocr(raw)
            ocr_pick = (ex or re.sub(r"\s+", " ", raw)[:900]).strip()
            if len(ocr_pick) >= 10:
                return ocr_pick
    parts = [(top.text or "").strip()]
    if (top.detail or "").strip():
        parts.append((top.detail or "").strip())
    return " ".join(p for p in parts if p).strip()


def _extra_story_ocr_text(monitor: FacebookPostMonitor | None) -> str:
    """仅返回首条截图 OCR 提取正文（无 OCR 时返回空）。"""
    if not monitor:
        return ""
    raw = (getattr(monitor, "last_top_post_story_ocr_text", "") or "").strip()
    if not raw:
        return ""
    ex = FacebookPostMonitor._extract_post_body_from_ocr(raw)
    body = (ex or re.sub(r"\s+", " ", raw)[:900]).strip()
    if not body:
        return ""
    body = re.sub(r"^[^A-Za-z0-9\u4e00-\u9fff]+", "", body).strip()
    # OCR 常把页头噪声（营业状态 / 标签栏 / 导航词）串到正文开头。
    # 这里循环剥离，直到没有任何已知前缀为止，避免 "Open now Closed now For sale..."
    # 之类被多前缀污染的情况只剥一次。
    _HEADER_NOISE_RE = re.compile(
        r"^("
        r"open\s+now|closed\s+now|open\s+until[^A-Za-z]*\d{0,2}[:\.]?\d{0,2}\s*(?:am|pm)?|"
        r"opens?\s+at\s+\d{1,2}[:\.]?\d{0,2}\s*(?:am|pm)?|"
        r"closes?\s+at\s+\d{1,2}[:\.]?\d{0,2}\s*(?:am|pm)?|"
        r"open\s+24\s*hours?|"
        r"permanently\s+closed|temporarily\s+closed|"
        r"following|follow|message|share|contact|like|liked|"
        r"search|filters?|about|photos|videos|reviews?|reels|"
        r"followers?|mentions|posts|home|menu|"
        r"hooked\s+billiards"
        r")\b[\s:：\-—|·•]*",
        re.I,
    )
    for _ in range(8):  # 最多剥 8 层前缀就够了
        new_body = _HEADER_NOISE_RE.sub("", body).strip()
        if new_body == body:
            break
        body = new_body
    body = re.sub(r"\s+", " ", body).strip()
    return body if len(body) >= 10 else ""


def _extra_story_ocr_time(monitor: FacebookPostMonitor | None, body: str = "") -> str:
    """从 OCR 原文中提取帖子时间（优先正文上一行）。"""
    if not monitor:
        return ""
    raw = (getattr(monitor, "last_top_post_story_ocr_text", "") or "").strip()
    if not raw:
        return ""
    lines = [re.sub(r"\s+", " ", ln).strip(" -|•·\t") for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    time_like = re.compile(
        r"(yesterday\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?|"
        r"today\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?|"
        r"just now|"
        r"\b\d+\s*(?:s|m|h|d|w)\b|"
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?\b|"
        r"昨天|今天|刚刚)",
        re.I,
    )

    body_norm = re.sub(r"\s+", " ", (body or "").strip().lower())
    if body_norm:
        for i, ln in enumerate(lines):
            ln_norm = re.sub(r"\s+", " ", ln.lower())
            if body_norm[:36] and body_norm[:36] in ln_norm:
                for j in (i - 1, i - 2):
                    if 0 <= j < len(lines) and time_like.search(lines[j]):
                        m = time_like.search(lines[j])
                        return re.sub(r"\s+", " ", (m.group(0) if m else lines[j])).strip()[:90]

    for ln in lines[:12]:
        if time_like.search(ln):
            m = time_like.search(ln)
            return re.sub(r"\s+", " ", (m.group(0) if m else ln)).strip()[:90]
    return ""


def _extra_story_ocr_payload(monitor: FacebookPostMonitor | None) -> tuple[str, str]:
    """返回 (ocr_time, ocr_text)。"""
    body = _extra_story_ocr_text(monitor)
    ocr_time = _extra_story_ocr_time(monitor, body=body)
    return ocr_time, body


def _pkill_playwright_and_browser() -> None:
    """Kill Playwright's Node.js driver and browser processes at OS level.

    Playwright's sync API is NOT thread-safe: calling monitor.close() from a background
    thread doesn't work because close() needs the main thread's event loop. This function
    bypasses Python entirely — pkill sends SIGKILL directly to the OS processes, which
    breaks the IPC pipe and causes the main thread's stuck Playwright call to raise an
    exception immediately.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    killed_any = False
    # Kill Playwright's Node.js driver (blocks all IPC, unblocks every pending call)
    r = subprocess.run(
        ["pkill", "-9", "-f", "playwright/driver/node"],
        capture_output=True,
    )
    if r.returncode == 0:
        killed_any = True
    # Kill Chromium browser processes ("Google Chrome for Testing" on macOS)
    r = subprocess.run(
        ["pkill", "-9", "-f", "ms-playwright"],
        capture_output=True,
    )
    if r.returncode == 0:
        killed_any = True
    if killed_any:
        print(f"[{ts}] watchdog_bg: force-killed playwright/browser OS processes", flush=True)


def _reset_playwright_python_state(extra_sources: list[dict]) -> None:
    """Reset Playwright's Python-side shared state after OS-level process kill.

    After pkill kills the Node.js driver and browser, the main thread may still call
    close() → playwright.stop(), which goes through the sync wrapper and tries to reach
    the already-dead event loop, hanging indefinitely. By zeroing out the shared class
    variables here, close() sees None everywhere and exits immediately without any IPC.
    The next _ensure_browser() call then creates a completely fresh Playwright instance.

    This is safe because we already killed the OS processes — there is nothing left to
    communicate with. Any race with the main thread is benign: the worst case is that
    main reads a non-None value and tries one more IPC call that fails with an exception.
    """
    from src.fb_monitor import FacebookPostMonitor  # local import to avoid circular
    FacebookPostMonitor._shared_playwright = None
    FacebookPostMonitor._shared_persistent_context = None
    FacebookPostMonitor._shared_browser = None
    FacebookPostMonitor._persistent_context_users = 0
    FacebookPostMonitor._shared_context_users = 0
    for source in extra_sources:
        mon = source["monitor"]
        mon._page = None   # type: ignore[attr-defined]
        mon._context = None  # type: ignore[attr-defined]


def _start_screenshot_watchdog_thread(
    extra_sources: list[dict],
    cfg: AppConfig,
    stop_event: threading.Event,
) -> threading.Thread:
    """Background watchdog: checks screenshot age every 30s independently of the main poll loop.

    The loop-based watchdog only runs at the start of each iteration. If fetch_posts() hangs
    inside Playwright, that watchdog never fires. This thread fires regardless.

    Recovery strategy (two-stage):
    - 1st fire (age >= stale_threshold): pkill Playwright/browser processes. This breaks the
      IPC pipe, which raises an exception in the main thread and unblocks it. monitor.close()
      is NOT called here because Playwright's sync API is not thread-safe — calling it from a
      background thread can't reach the main thread's event loop and does nothing.
    - 2nd+ fire (still stale after pkill): pkill again; also send a push notification so the
      user can manually verify no posts were missed.
    """
    stale_threshold = max(60, int(getattr(cfg, "screenshot_stale_seconds", 150) or 150))
    cooldown = 180
    alert_threshold = max(stale_threshold, 300)
    alert_cooldown = 1800  # at most one stuck-browser alert every 30 minutes

    def _loop() -> None:
        # Start with a full cooldown so the background watchdog doesn't fire immediately
        # on startup when the screenshot is already stale (the loop-based watchdog and
        # _ensure_browser handle first-run recovery; this thread only kicks in for hangs
        # that occur AFTER the browser is already running).
        last_recover_ts = time.time()
        last_alert_ts = 0.0
        consecutive_fires = 0

        while not stop_event.wait(30):
            age = _screenshot_age_seconds()
            if age is None or age < stale_threshold:
                consecutive_fires = 0  # screenshot is being updated — things are healthy
                continue
            now = time.time()
            if now - last_recover_ts < cooldown:
                continue

            consecutive_fires += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] watchdog_bg: screenshot {age}s stale (fire #{consecutive_fires}) "
                f"— force-killing playwright/browser processes",
                flush=True,
            )
            _pkill_playwright_and_browser()
            _reset_playwright_python_state(extra_sources)
            last_recover_ts = now

            # If pkill + state-reset haven't unblocked the main thread by fire #2 (~6 min
            # stuck), Playwright's Python layer is in an unrecoverable deadlock. Self-SIGKILL
            # so LaunchAgent restarts a clean process. All watcher state is persisted to disk.
            if consecutive_fires >= 2:
                print(
                    f"[{ts}] watchdog_bg: pkill has not unblocked main thread after "
                    f"{consecutive_fires} attempts — sending SIGKILL to self for clean restart",
                    flush=True,
                )
                os.kill(os.getpid(), signal.SIGKILL)
                return  # unreachable, but makes intent clear

            # Send alert notification on the 2nd+ fire (pkill on 1st fire should have
            # unblocked the main thread; still stuck means a persistent problem).
            if (
                consecutive_fires >= 2
                and age >= alert_threshold
                and now - last_alert_ts >= alert_cooldown
                and cfg.notify_enabled
            ):
                target_url = (cfg.extra_target_urls[0] if cfg.extra_target_urls else cfg.target_url) or ""
                try:
                    notify_new_post(
                        title="⚠️ Facebook 监控浏览器卡死，请人工确认",
                        message=(
                            f"截图已 {age // 60} 分 {age % 60} 秒未更新，监控程序已自动重建浏览器会话。\n"
                            f"若此期间有新帖发出，可能存在遗漏，请手动查看：{target_url}"
                        ),
                        url=target_url,
                        open_in_browser=False,
                        wecom_webhook_url=cfg.wecom_webhook_url,
                        serverchan_sendkey=cfg.serverchan_sendkey,
                        smtp_host=cfg.smtp_host,
                        smtp_port=cfg.smtp_port,
                        smtp_username=cfg.smtp_username,
                        smtp_password=cfg.smtp_password,
                        smtp_from=cfg.smtp_from,
                        email_to=cfg.email_to,
                        telegram_bot_token=cfg.telegram_bot_token,
                        telegram_chat_ids=cfg.telegram_chat_ids,
                    )
                    print(
                        f"[{ts}] watchdog_bg: stuck-browser alert sent ({age}s stale)",
                        flush=True,
                    )
                    last_alert_ts = now
                except Exception as exc:
                    print(
                        f"[{ts}] watchdog_bg: stuck-browser alert failed: {exc}",
                        flush=True,
                    )

    t = threading.Thread(target=_loop, daemon=True, name="screenshot-watchdog")
    t.start()
    return t


_ERROR_PAGE_PATTERNS = [
    "we are working on getting this fixed",
    "something went wrong",
    "this page isn't available",
    "this content isn't available",
    "page not found",
    "sorry, this content isn't available",
    "we suspect automated behavior",
    "temporarily restricted",
    "you must log in",
    "log in to facebook",
    "must log in to see",
    "see more on facebook",
    "try again later",
    "couldn't load",
    "an error occurred",
    "please try again",
    "正在修复",
    "出错了",
    "暂时受限",
    "该内容暂时无法显示",
    "此页面无法使用",
    "请稍后再试",
]


def _is_ocr_error_page(ocr_text: str) -> bool:
    """Return True if OCR text looks like an error/login page rather than real post content."""
    if not ocr_text or len(ocr_text.strip()) < 10:
        return False
    low = ocr_text.lower()
    return any(p in low for p in _ERROR_PAGE_PATTERNS)


def _is_ocr_garbage(ocr_text: str) -> bool:
    """Return True if OCR text looks like random character noise rather than real content."""
    if not ocr_text or len(ocr_text.strip()) < 10:
        return False
    words = re.findall(r"[A-Za-z]{3,}", ocr_text)
    alpha_chars = sum(1 for c in ocr_text if c.isalpha())
    total_chars = len(ocr_text.strip())
    if total_chars == 0:
        return True
    if alpha_chars / total_chars < 0.4 and len(words) < 3:
        return True
    if len(words) < 2 and not re.search(r"[\u4e00-\u9fff]", ocr_text):
        return True
    return False


def _extra_story_content_sig(top: PostItem, monitor: FacebookPostMonitor | None) -> str:
    """首条 OCR 指纹：仅正文（时间仅用于日志展示，不触发通知）。"""
    _ocr_time, body = _extra_story_ocr_payload(monitor)
    norm = re.sub(r"\s+", " ", body.lower())[:2800]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest() if norm else ""


def _visual_phash_hamming(a: str, b: str) -> int:
    """64-bit average hash hex (16 chars); Hamming distance. Mismatch shape → large distance."""
    if not a or not b or len(a) != 16 or len(b) != 16:
        return 999
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 999


def _extra_feed_multimodal_step(
    monitor: FacebookPostMonitor,
    state: StateStore,
    cfg,
    source_label: str,
    *,
    dom_has_fresh_posts: bool,
) -> None:
    """页顶截图哈希 + 可选 OCR 文本：与上一轮对比；DOM 无新帖时可合并推送。"""
    ts = datetime.now().strftime("%H:%M:%S")
    notify_lines: list[str] = []
    want_notify = False

    vis = (getattr(monitor, "_last_feed_top_visual_hash", "") or "").strip()
    if cfg.extra_feed_visual_enabled and vis:
        prev = state.get_extra_feed_top_phash()
        if prev:
            dist = _visual_phash_hamming(vis, prev)
            if dist >= cfg.extra_feed_visual_min_hamming:
                print(
                    f"[{ts}] {source_label} feed_visual_changed hamming={dist} "
                    f"(页顶截图指纹变化，可能有新帖或广告/动画干扰)",
                    flush=True,
                )
                notify_lines.append(f"截图指纹变化约 {dist} bit")
                if cfg.extra_feed_visual_notify:
                    want_notify = True
        state.set_extra_feed_top_phash(vis)

    ocr_raw = (getattr(monitor, "_last_feed_top_ocr_text", "") or "").strip()
    ocr_norm = re.sub(r"\s+", " ", ocr_raw.lower())[:1200] if ocr_raw else ""
    if cfg.extra_feed_ocr_enabled and len(ocr_norm) > 40:
        prev_o = state.get_extra_feed_top_ocr_norm()
        if prev_o and prev_o != ocr_norm:
            print(
                f"[{ts}] {source_label} feed_ocr_changed "
                f"(页顶 OCR 文本相对上一轮有变化，节选 {len(ocr_norm)} 字)",
                flush=True,
            )
            notify_lines.append("页顶 OCR 文本与上一轮不同（节选）:")
            notify_lines.append(ocr_norm[:480])
            if cfg.extra_feed_ocr_notify:
                want_notify = True
        state.set_extra_feed_top_ocr_norm(ocr_norm)

    if (
        want_notify
        and notify_lines
        and cfg.notify_enabled
        and not dom_has_fresh_posts
    ):
        notify_new_post(
            title="Facebook 页顶多信号变化",
            message="\n".join(notify_lines) + f"\n\n{monitor.target_url}",
            url=monitor.target_url,
            open_in_browser=cfg.open_on_alert,
            wecom_webhook_url=cfg.wecom_webhook_url,
            serverchan_sendkey=cfg.serverchan_sendkey,
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_username=cfg.smtp_username,
            smtp_password=cfg.smtp_password,
            smtp_from=cfg.smtp_from,
            email_to=cfg.email_to,
            telegram_bot_token=cfg.telegram_bot_token,
            telegram_chat_ids=cfg.telegram_chat_ids,
        )


def _log_fetched_latest(
    source_label: str,
    posts: list[PostItem],
    monitor: FacebookPostMonitor | None = None,
) -> None:
    """每轮只打 OCR 正文（按需求：日志仅保留 OCR 文本）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    if not posts or not monitor:
        print(f"[{ts}] {source_label} latest_post_text: (none)", flush=True)
        return
    ocr_time, body = _extra_story_ocr_payload(monitor)
    body = body or "(无 OCR 正文)"
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > 900:
        body = body[:898] + "…"
    tline = re.sub(r"\s+", " ", (ocr_time or "(unknown)")).strip()
    if len(tline) > 120:
        tline = tline[:118] + "…"
    print(f"[{ts}] {source_label} latest_post_time: {tline}", flush=True)
    print(f"[{ts}] {source_label} latest_post_text: {body}", flush=True)


def _chat_window_url(profile_url: str) -> str:
    parsed = urlparse(profile_url.strip())
    if "facebook.com" not in (parsed.netloc or "").lower():
        return ""
    path = (parsed.path or "").strip("/")
    if not path:
        return ""
    first_segment = path.split("/", 1)[0]
    if first_segment in {"profile.php", "marketplace", "groups", "pages"}:
        return ""
    return f"https://m.me/{first_segment}"


def _age_seconds_from_facebook_time_label(raw: str | None) -> int | None:
    """从时间文案估算「距发帖约多少秒」；无法识别则 None。"""
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().lower()
    s_oneline = re.sub(r"\s+", " ", s.split("\n")[0].strip())
    if re.search(r"just now|^now$|刚刚|片刻", s_oneline):
        return 0
    if "yesterday" in s or "昨天" in s:
        return int(36 * 3600)
    # 4d / 19h / 45m / 12w（常见无空格）
    m = re.search(
        r"\b(\d+)\s*(s|sec|secs|second|seconds|秒)\b",
        s_oneline,
        re.I,
    )
    if m:
        return min(int(m.group(1)), 120)
    m = re.search(
        r"\b(\d+)\s*(m|min|mins|minute|minutes|分钟)\b",
        s_oneline,
        re.I,
    )
    if m:
        return min(int(m.group(1)) * 60, 86400)
    m = re.search(r"\b(\d+)\s*(h|hr|hrs|hour|hours|小时)\b", s_oneline, re.I)
    if m:
        return min(int(m.group(1)) * 3600, 86400 * 14)
    m = re.search(r"\b(\d+)\s*(d|day|days|天)\b", s_oneline, re.I)
    if m:
        return int(m.group(1)) * 86400
    m = re.search(r"\b(\d+)\s*(w|wk|week|weeks|周)\b", s_oneline, re.I)
    if m:
        return min(int(m.group(1)) * 86400 * 7, 86400 * 120)
    m = re.search(r"\b(\d+)\s*([smhdw])\b", s_oneline, re.I)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 86400 * 7}.get(u, 0)
        if mult:
            return min(n * mult, 86400 * 120)
    return None


def _extra_post_effective_age_seconds(post: PostItem) -> int | None:
    """距发帖的估计秒数；None 表示无法判断（不推送）。"""
    now = int(time.time())
    if post.posted_utime > 0:
        age = now - post.posted_utime
        return max(0, age)
    return _age_seconds_from_facebook_time_label(post.posted_at)


def _extra_post_is_recent_enough(post: PostItem, max_age_seconds: int) -> bool:
    """Extra 推送：data-utime 或时间文案（如 2h、Yesterday）在窗口内；避免仅靠误抓的旧 utime。"""
    age = _extra_post_effective_age_seconds(post)
    if age is None:
        return False
    return age <= max_age_seconds


def _poll_extra_source(
    monitor: FacebookPostMonitor,
    state: StateStore,
    cfg,
    source_label: str,
    initialized: bool,
) -> tuple[bool, int]:
    posts = monitor.fetch_posts()
    latest_posts = posts[:1]
    latest_urls = {p.post_id: p.url for p in latest_posts}
    if not initialized:
        state.mark_seen([p.post_id for p in posts])
        state.set_active_ids([p.post_id for p in latest_posts])
        state.upsert_post_urls(latest_urls)
        _log_fetched_latest(source_label, posts, monitor)
        if posts:
            raw_init_ocr = (getattr(monitor, "last_top_post_story_ocr_text", "") or "").strip()
            init_body = _extra_story_ocr_text(monitor)
            if _is_ocr_error_page(raw_init_ocr) or _is_ocr_error_page(init_body):
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{ts}] {source_label} 初始化时 OCR 检测到错误页面，不存储签名：{init_body[:120]}",
                    flush=True,
                )
            else:
                state.set_extra_top_post_url((posts[0].url or "").strip())
                state.set_extra_last_story_content_sig(_extra_story_content_sig(posts[0], monitor))
                state.set_extra_last_story_clip_hash(
                    (getattr(monitor, "last_top_post_story_clip_hash", "") or "").strip()
                )
        return True, len(posts)

    seen = state.seen_ids
    new_posts = [p for p in posts if p.post_id not in seen]
    if new_posts:
        state.mark_seen([p.post_id for p in new_posts])
    state.set_active_ids([p.post_id for p in latest_posts])
    state.upsert_post_urls(latest_urls)

    _log_fetched_latest(source_label, posts, monitor)
    if posts:
        top = posts[0]
        ocr_time, ocr_body = _extra_story_ocr_payload(monitor)
        disp = ocr_body

        raw_ocr = (getattr(monitor, "last_top_post_story_ocr_text", "") or "").strip()
        if _is_ocr_error_page(raw_ocr) or _is_ocr_error_page(disp):
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] {source_label} OCR 检测到错误/非帖子页面，跳过比对，不更新签名：{disp[:120]}",
                flush=True,
            )
            state.set_extra_pending_content_sig("", 0)
            return True, len(posts)

        if _is_ocr_garbage(disp):
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] {source_label} OCR 检测到乱码，跳过比对：{disp[:120]}",
                flush=True,
            )
            state.set_extra_pending_content_sig("", 0)
            return True, len(posts)

        # FB 抓取/截图/OCR 静默失败 → 本轮没有任何可比对正文。
        # 必须直接返回，**绝不能** 用空 sig 覆盖 state.last_story_content_sig，
        # 否则下一次真有新帖（prev_sig 为空）会被当成首次初始化而漏推。
        if not raw_ocr and not disp:
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] {source_label} OCR 抓取失败/为空（last_top_post_story_ocr_text 为空），"
                f"跳过本轮比对与签名写入；本轮按抓取失败计入会话健康度",
                flush=True,
            )
            # initialized 保持 True（已初始化），但 fetched_count 报 0 → cycle_source_ok=False，
            # 上层 session_alert / 浏览器重连看护可据此触发。
            return True, 0

        new_sig = _extra_story_content_sig(top, monitor)
        prev_sig = state.get_extra_last_story_content_sig()

        _CONFIRM_ROUNDS = 1
        if initialized and prev_sig and new_sig and prev_sig != new_sig and len((disp or "").strip()) >= 10:
            pending_sig = state.get_extra_pending_content_sig()
            pending_count = state.get_extra_pending_sig_count()
            if new_sig == pending_sig:
                pending_count += 1
            else:
                pending_count = 1
            state.set_extra_pending_content_sig(new_sig, pending_count)

            if pending_count >= _CONFIRM_ROUNDS:
                ts = datetime.now().strftime("%H:%M:%S")
                prev_notified_ocr_time = state.get_extra_last_notified_ocr_time()
                # 同一个帖子的 OCR 渲染噪声（按钮/评论区变化）不通知，只有帖子时间变了才算新帖
                same_post = bool(
                    ocr_time and prev_notified_ocr_time and ocr_time == prev_notified_ocr_time
                )
                if same_post:
                    print(
                        f"[{ts}] {source_label} OCR sig 变化但帖子时间未变（{ocr_time!r}），忽略（渲染噪声）",
                        flush=True,
                    )
                    state.set_extra_last_story_content_sig(new_sig)
                    state.set_extra_pending_content_sig("", 0)
                else:
                    print(
                        f"[{ts}] {source_label} OCR 文本变更已确认（连续 {pending_count} 轮一致）",
                        flush=True,
                    )
                    if cfg.notify_enabled:
                        msg = (disp or "(OCR 为空)")[:1200]
                        if ocr_time:
                            msg = f"{ocr_time}\n{msg}"
                        notify_new_post(
                            title="Facebook OCR 文本变更",
                            message=msg,
                            url=monitor.target_url,
                            open_in_browser=cfg.open_on_alert,
                            wecom_webhook_url=cfg.wecom_webhook_url,
                            serverchan_sendkey=cfg.serverchan_sendkey,
                            smtp_host=cfg.smtp_host,
                            smtp_port=cfg.smtp_port,
                            smtp_username=cfg.smtp_username,
                            smtp_password=cfg.smtp_password,
                            smtp_from=cfg.smtp_from,
                            email_to=cfg.email_to,
                            telegram_bot_token=cfg.telegram_bot_token,
                            telegram_chat_ids=cfg.telegram_chat_ids,
                        )
                    if ocr_time:
                        state.set_extra_last_notified_ocr_time(ocr_time)
                    state.set_extra_last_story_content_sig(new_sig)
                    state.set_extra_pending_content_sig("", 0)
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{ts}] {source_label} OCR 文本疑似变更，等待确认（{pending_count}/{_CONFIRM_ROUNDS}）：{disp[:80]}",
                    flush=True,
                )
        else:
            if new_sig == prev_sig:
                state.set_extra_pending_content_sig("", 0)
            state.set_extra_last_story_content_sig(new_sig)
    return True, len(posts)


def main() -> None:
    _acquire_pid_lock()
    cfg = load_config()
    monitor = (
        FacebookPostMonitor(
            cfg.target_url,
            timeout_seconds=cfg.request_timeout_seconds,
            cookie=cfg.facebook_cookie,
            accept_language=cfg.fb_accept_language,
            browser_user_data_dir=cfg.fb_browser_user_data_dir,
            browser_headless=cfg.fb_browser_headless,
            fb_email=cfg.fb_email,
            fb_password=cfg.fb_password,
        )
        if cfg.target_url
        else None
    )
    if monitor:
        _active_monitors.append(monitor)
    state = StateStore(cfg.state_file)

    print("Facebook Post Watcher started")
    _eh = cfg.extra_new_post_max_age_seconds / 3600.0
    print(
        f"Extra 主页：未见过且符合 /用户名/posts/… 的链接记入 seen；"
        f"发帖时间在最近 {_eh:g} 小时内才推送（data-utime 或时间文案如 2h、Yesterday 估算；"
        f"两者都无法判断则不推）。",
        flush=True,
    )
    if cfg.target_url:
        print(f"Target: {cfg.target_url}")
    else:
        print("Target: (disabled)")
    print(f"Interval: {cfg.check_interval_seconds}s")
    if monitor and monitor.use_browser_mode:
        print("Fetch mode: Playwright browser")
    if cfg.match_keywords:
        print(f"Keywords: {cfg.match_keywords}")
    if cfg.fb_browser_user_data_dir:
        print(f"Facebook session: persistent profile ({cfg.fb_browser_user_data_dir})")
        print(f"Browser headless: {cfg.fb_browser_headless}")
    elif cfg.facebook_cookie:
        print("Facebook cookie auth: enabled")
    if cfg.wecom_webhook_url:
        print("WeChat push: WeCom bot enabled")
    if cfg.serverchan_sendkey:
        print("WeChat push: ServerChan enabled")
    if cfg.telegram_bot_token and cfg.telegram_chat_ids:
        print(f"Telegram push: enabled ({len(cfg.telegram_chat_ids)} chat(s))")
    if not _can_push_session_alert(cfg):
        print(
            "Warning: 未配置 WECOM_WEBHOOK_URL / SERVERCHAN / SMTP / TELEGRAM，"
            "Cookie 或登录失效时将无法收到推送"
        )

    extra_sources: list[dict] = []
    for idx, extra_url in enumerate(cfg.extra_target_urls, start=1):
        _extra_mon = FacebookPostMonitor(
            extra_url,
            timeout_seconds=cfg.request_timeout_seconds,
            cookie=cfg.facebook_cookie,
            accept_language=cfg.fb_accept_language,
            browser_user_data_dir=cfg.fb_browser_user_data_dir,
            browser_headless=cfg.fb_browser_headless,
            fb_email=cfg.fb_email,
            fb_password=cfg.fb_password,
            feed_visual_clip_height=cfg.extra_feed_visual_clip_height,
            extra_feed_ocr_enabled=cfg.extra_feed_ocr_enabled,
            extra_feed_ocr_lang=cfg.extra_feed_ocr_lang,
            profile_feed_scroll_passes=cfg.extra_profile_feed_scroll_passes,
        )
        _active_monitors.append(_extra_mon)
        extra_sources.append(
            {
                "label": f"extra_{idx}",
                "monitor": _extra_mon,
                "state": StateStore(_extra_state_path(cfg.state_file, idx)),
                "initialized": False,
                # 连续多少轮 OCR 抓取失败（fetched_count=0）。达到阈值就强制重建浏览器会话。
                "ocr_fail_streak": 0,
            }
        )
        print(f"Extra target[{idx}]: {extra_url}")
    if cfg.extra_target_urls:
        print(
            f"Extra 时间线滚动兜底: EXTRA_PROFILE_FEED_SCROLL_PASSES={cfg.extra_profile_feed_scroll_passes} "
            f"（0=不向下滚，仅首屏+视口首条+OCR；>0 时多滚几屏并合并 /posts 列表）",
            flush=True,
        )
    if cfg.extra_feed_visual_enabled:
        print(
            f"Extra 页顶截图指纹: 已启用（高度 {cfg.extra_feed_visual_clip_height}px，"
            f"Hamming≥{cfg.extra_feed_visual_min_hamming} 视为变化"
            + ("；变化时推送" if cfg.extra_feed_visual_notify else "")
            + "）",
            flush=True,
        )
    if cfg.extra_feed_ocr_enabled:
        print(
            f"Extra 页顶 OCR: 已启用（需 pip install pytesseract 且本机安装 Tesseract；lang={cfg.extra_feed_ocr_lang!r}"
            + ("；变化时推送" if cfg.extra_feed_ocr_notify else "")
            + "）",
            flush=True,
        )

    initialized = False
    cookie_fail_cycles = 0
    cookie_alert_sent = False
    last_force_recover_ts = 0.0
    _watchdog_stop = threading.Event()
    if extra_sources:
        _start_screenshot_watchdog_thread(extra_sources, cfg, _watchdog_stop)
    while True:
        try:
            _maybe_truncate_watcher_out_log()
            last_force_recover_ts = _screenshot_watchdog_check(
                extra_sources, cfg, last_force_recover_ts
            )
            print(f"[{datetime.now().strftime('%H:%M:%S')}] --- poll cycle ---", flush=True)
            sys.stdout.flush()
            posts: list[PostItem] = []
            if monitor:
                try:
                    posts = monitor.fetch_posts()
                except Exception as exc:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] primary fetch error: {exc}")
                    _recover_browser_monitor(monitor, "primary")
                    posts = []
                post_by_id = {p.post_id: p for p in posts}
                signatures = {p.post_id: _post_signature(p) for p in posts}
                post_urls = {p.post_id: p.url for p in posts}
                current_ids = set(post_by_id.keys())

                if not initialized:
                    seed_ids = [p.post_id for p in posts]
                    state.mark_seen(seed_ids)
                    state.upsert_snapshots(signatures)
                    state.set_active_ids(seed_ids)
                    state.set_missing_counts({})
                    state.set_candidate_counts({})
                    state.upsert_post_urls(post_urls)
                    state.set_inactive_ids([])
                    state.set_change_candidates({})
                    state.set_notified_change_sigs({})
                    state.set_event_last_sent({})
                    initialized = True
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Initialized with {len(seed_ids)} posts",
                        flush=True,
                    )
                    _log_fetched_latest("primary", posts, monitor)
                else:
                    seen = state.seen_ids
                    previous_active_ids = state.active_ids
                    previous_urls = state.post_urls
                    previous_snapshots = state.snapshots
                    previous_inactive_ids = state.inactive_ids
                    previous_notified_change_sigs = state.notified_change_sigs

                    new_posts = [p for p in posts if p.post_id not in seen]
                    confirmed_new_posts: list[PostItem] = list(new_posts)
                    next_candidate_counts: dict[str, int] = {}

                    relisted_posts = [p for p in posts if p.post_id in previous_inactive_ids]
                    relisted_ids = {p.post_id for p in relisted_posts}
                    confirmed_new_ids = {p.post_id for p in confirmed_new_posts}

                    raw_changed_posts = [
                        p
                        for p in posts
                        if p.post_id in previous_snapshots
                        and signatures[p.post_id] != previous_snapshots[p.post_id]
                        and p.post_id not in relisted_ids
                        and p.post_id not in confirmed_new_ids
                    ]
                    next_change_candidates: dict[str, dict[str, int | str]] = {}
                    changed_posts: list[PostItem] = list(raw_changed_posts)
                    next_stable_snapshots = dict(previous_snapshots)
                    for post in posts:
                        post_id = post.post_id
                        sig = signatures[post_id]
                        old_sig = previous_snapshots.get(post_id)
                        if old_sig is None:
                            next_stable_snapshots[post_id] = sig
                            continue
                        if sig == old_sig:
                            continue
                        next_stable_snapshots[post_id] = sig

                    removed_candidates = previous_active_ids - current_ids

                    previous_missing_counts = state.missing_counts
                    missing_counts: dict[str, int] = {}
                    for post_id in removed_candidates:
                        missing_counts[post_id] = previous_missing_counts.get(post_id, 0) + 1
                    newly_inactive_ids = {post_id for post_id, count in missing_counts.items() if count >= 1}
                    next_inactive_ids = (previous_inactive_ids - current_ids) | newly_inactive_ids

                    matched_new = [p for p in confirmed_new_posts if _match_keywords(p, cfg.match_keywords)]
                    matched_relisted = [p for p in relisted_posts if _match_keywords(p, cfg.match_keywords)]
                    matched_changed = []
                    for post in changed_posts:
                        if not _match_keywords(post, cfg.match_keywords):
                            continue
                        if not _is_valid_changed_item(post):
                            continue
                        # Pending-review listings are noisy before approval; only notify once on creation.
                        if _is_pending_review_item(post):
                            continue
                        new_change_key = _change_event_key(post)
                        old_notified_sig = previous_notified_change_sigs.get(post.post_id, "")
                        if old_notified_sig == new_change_key:
                            continue
                        matched_changed.append(post)
                    matched_removed = sorted(newly_inactive_ids)
                    detail_lines: list[str] = []
                    first_url = cfg.target_url
                    for post in reversed(matched_new):
                        item_name = _item_name(post)
                        detail_lines.append(f"上架: {item_name}\nURL: {post.url}")
                        if first_url == cfg.target_url:
                            first_url = post.url

                    for post in reversed(matched_relisted):
                        item_name = _item_name(post)
                        detail_lines.append(f"重新上架: {item_name}\nURL: {post.url}")
                        if first_url == cfg.target_url:
                            first_url = post.url

                    for post in reversed(matched_changed):
                        item_name = _item_name(post)
                        detail_lines.append(f"编辑变化: {item_name}\nURL: {post.url}")
                        if first_url == cfg.target_url:
                            first_url = post.url

                    for post_id in matched_removed:
                        old_url = previous_urls.get(post_id, cfg.target_url)
                        item_code = _item_code_from_url(old_url)
                        detail_lines.append(f"下架/无货: item/{item_code}\nURL: {old_url}")
                        if first_url == cfg.target_url:
                            first_url = old_url

                    if detail_lines:
                        preview = "\n".join(detail_lines[:8])
                        extra = len(detail_lines) - 8
                        if extra > 0:
                            preview = f"{preview}\n... 还有 {extra} 条"
                        notify_new_post(
                            title="Facebook 商品店",
                            message=preview,
                            url=first_url,
                            open_in_browser=cfg.open_on_alert and len(matched_new) > 0,
                            wecom_webhook_url=cfg.wecom_webhook_url,
                            serverchan_sendkey=cfg.serverchan_sendkey,
                            smtp_host=cfg.smtp_host,
                            smtp_port=cfg.smtp_port,
                            smtp_username=cfg.smtp_username,
                            smtp_password=cfg.smtp_password,
                            smtp_from=cfg.smtp_from,
                            email_to=cfg.email_to,
                            telegram_bot_token=cfg.telegram_bot_token,
                            telegram_chat_ids=cfg.telegram_chat_ids,
                        )
                        print("Details:")
                        for line in detail_lines:
                            print(f"- {line}")

                    if confirmed_new_posts:
                        state.mark_seen([p.post_id for p in confirmed_new_posts])
                    state.set_active_ids(list(current_ids))
                    state.set_missing_counts(missing_counts)
                    state.set_candidate_counts(next_candidate_counts)
                    state.upsert_post_urls(post_urls)
                    state.set_inactive_ids(list(next_inactive_ids))
                    state.set_change_candidates(next_change_candidates)
                    next_notified_change_sigs = dict(previous_notified_change_sigs)
                    for post in matched_changed:
                        next_notified_change_sigs[post.post_id] = _change_event_key(post)
                    state.set_notified_change_sigs(next_notified_change_sigs)
                    state.set_event_last_sent({})
                    state.upsert_snapshots(next_stable_snapshots)

                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"fetched={len(posts)} new={len(new_posts)} "
                        f"relisted={len(relisted_posts)} changed={len(changed_posts)} "
                        f"inactive={len(next_inactive_ids)} "
                        f"matched_new={len(matched_new)} matched_relisted={len(matched_relisted)} "
                        f"matched_changed={len(matched_changed)} matched_removed={len(matched_removed)} "
                        f"pending_new={len(next_candidate_counts)}",
                        flush=True,
                    )
                    _log_fetched_latest("primary", posts, monitor)

            session_login_wall = False
            screenshot_persistent_failure_detected = False
            cycle_source_ok: list[bool] = []
            if monitor and monitor.use_browser_mode:
                primary_ok = len(posts) > 0
                cycle_source_ok.append(primary_ok)
                if not primary_ok and monitor.last_login_wall_hint:
                    session_login_wall = True
                if getattr(monitor, "last_screenshot_persistent_failure", False):
                    screenshot_persistent_failure_detected = True
            for source in extra_sources:
                attempts = 2 if cfg.fetch_error_auto_retry else 1
                last_attempt_ok = False
                for attempt in range(attempts):
                    try:
                        source_initialized, fetched_count = _poll_extra_source(
                            monitor=source["monitor"],
                            state=source["state"],
                            cfg=cfg,
                            source_label=source["label"],
                            initialized=bool(source["initialized"]),
                        )
                        source["initialized"] = source_initialized
                        extra_ok = fetched_count > 0
                        last_attempt_ok = extra_ok
                        cycle_source_ok.append(extra_ok)
                        if not extra_ok and source["monitor"].last_login_wall_hint:
                            session_login_wall = True
                        if getattr(source["monitor"], "last_screenshot_persistent_failure", False):
                            screenshot_persistent_failure_detected = True
                        break
                    except Exception as exc:
                        print(
                            f"[{datetime.now().strftime('%H:%M:%S')}] "
                            f"{source['label']} error (try {attempt + 1}/{attempts}): {exc}",
                            flush=True,
                        )
                        _recover_browser_monitor(source["monitor"], source["label"])
                        if attempt + 1 < attempts:
                            time.sleep(cfg.fetch_recovery_sleep_seconds)
                            continue
                        last_attempt_ok = False
                        cycle_source_ok.append(False)
                        if source["monitor"].last_login_wall_hint:
                            session_login_wall = True

                # 看护：连续 N 轮 OCR 失败（fetched_count=0）→ 浏览器很可能卡在僵尸/缓存页，
                # 强制 close()，下一轮 fetch_posts 会重建 page/context，触发自动重登。
                fail_limit = max(2, int(getattr(cfg, "ocr_fail_recover_after", 3) or 3))
                if last_attempt_ok:
                    source["ocr_fail_streak"] = 0
                else:
                    source["ocr_fail_streak"] = int(source.get("ocr_fail_streak", 0)) + 1
                    if source["ocr_fail_streak"] >= fail_limit:
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(
                            f"[{ts}] {source['label']} 连续 {source['ocr_fail_streak']} 轮 OCR 抓取为空/失败，"
                            f"强制重建浏览器会话",
                            flush=True,
                        )
                        _recover_browser_monitor(source["monitor"], source["label"])
                        source["ocr_fail_streak"] = 0

            if cycle_source_ok and all(not ok for ok in cycle_source_ok):
                cookie_fail_cycles += 1
                need = cfg.cookie_alert_consecutive_fails
                can_push = _can_push_session_alert(cfg)
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"session_check: round {cookie_fail_cycles}/{need} — "
                    "每个浏览器类目标本轮未解析到帖子或抓取失败。"
                    "这不等于 cookie 一定无效，也可能是主页暂无帖、FB 改版或限流。"
                    + (
                        f" 连续满 {need} 轮且已配置 WECOM_WEBHOOK_URL / SERVERCHAN_SENDKEY（或 COOKIE_ALERT_SENDKEY）/ SMTP 时，会发「Facebook 监听告警」。"
                        if can_push
                        else " 当前未配置上述任一外发渠道，会话告警不会发到手机。"
                    ),
                    flush=True,
                )
                global _SESSION_ALERT_NO_CHANNEL_LOGGED
                if not can_push and cookie_fail_cycles >= need and not _SESSION_ALERT_NO_CHANNEL_LOGGED:
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        "hint: 在 .env 里配置 SERVERCHAN_SENDKEY 或 WECOM_WEBHOOK_URL 后重启，"
                        "即可收到抓取全失败时的告警。",
                        flush=True,
                    )
                    _SESSION_ALERT_NO_CHANNEL_LOGGED = True
            else:
                cookie_fail_cycles = 0
                cookie_alert_sent = False

            if (
                _can_push_session_alert(cfg)
                and not cookie_alert_sent
                and cookie_fail_cycles >= cfg.cookie_alert_consecutive_fails
            ):
                serverchan = (cfg.cookie_alert_sendkey or cfg.serverchan_sendkey).strip()
                if cfg.fb_browser_user_data_dir:
                    fix_hint = (
                        "若浏览器里同一账号打开该主页能看到动态：多为解析/限流误报，可先观察日志；"
                        "若要求登录：请执行 `bash scripts/fb-browser-login.sh`，再 `scripts/restart-launchagent.sh`。"
                    )
                else:
                    fix_hint = (
                        "若浏览器已登录仍能看到主页动态：可能是误报；"
                        "否则请更新 FACEBOOK_COOKIE 并执行 `scripts/restart-launchagent.sh`。"
                    )
                if screenshot_persistent_failure_detected:
                    alert_title = "Facebook 截图持续失败，需要人工介入"
                    wall_line = (
                        "已自动尝试关闭浏览器 + 重新登录 + 重抓 3 次仍然无法拿到首条 story 截图。\n"
                        "极可能是：账号被风控/需要 2FA、FB 改了页面结构、"
                        "或本机网络代理出问题。请打开 https://www.facebook.com/ 用同一账号登录确认。\n"
                    )
                elif session_login_wall:
                    alert_title = "Facebook Cookie/登录失效"
                    wall_line = (
                        "页面检测为登录/验证界面，优先更新 FACEBOOK_COOKIE 或重新执行 fb-browser-login.sh。\n"
                    )
                else:
                    alert_title = "Facebook 监听告警"
                    wall_line = (
                        "（也可能是主页暂无动态、FB 改版或网络问题；请对照浏览器判断。）\n"
                    )
                notify_new_post(
                    title=alert_title,
                    message=(
                        f"连续 {cookie_fail_cycles} 轮：所有浏览器类监听目标未解析到帖子或抓取失败。\n"
                        f"{wall_line}"
                        f"{fix_hint}"
                    ),
                    url=cfg.extra_target_urls[0] if cfg.extra_target_urls else (cfg.target_url or "https://www.facebook.com"),
                    open_in_browser=False,
                    wecom_webhook_url=cfg.wecom_webhook_url,
                    serverchan_sendkey=serverchan,
                    smtp_host=cfg.smtp_host,
                    smtp_port=cfg.smtp_port,
                    smtp_username=cfg.smtp_username,
                    smtp_password=cfg.smtp_password,
                    smtp_from=cfg.smtp_from,
                    email_to=cfg.email_to,
                    telegram_bot_token=cfg.telegram_bot_token,
                    telegram_chat_ids=cfg.telegram_chat_ids,
                )
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    "session_alert: 已调用微信/ServerChan/邮件（见上方 notify 输出；若失败会有 wecom/serverchan push failed）",
                    flush=True,
                )
                cookie_alert_sent = True
        except KeyboardInterrupt:
            print("\nStopped by user.")
            _watchdog_stop.set()
            if monitor:
                monitor.close()
            for source in extra_sources:
                source["monitor"].close()
            break
        except Exception as exc:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] error: {exc}", flush=True)
            _recover_browser_monitor(monitor, "primary")
            for source in extra_sources:
                _recover_browser_monitor(source["monitor"], source["label"])

        _jitter = random.uniform(-0.25, 0.4) * cfg.check_interval_seconds
        time.sleep(max(10, cfg.check_interval_seconds + _jitter))


if __name__ == "__main__":
    main()
