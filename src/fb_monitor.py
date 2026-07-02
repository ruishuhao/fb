from __future__ import annotations

import fnmatch
import hashlib
import io
import re
import subprocess
import sys
import time
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def _fb_label_age_seconds(raw: str | None) -> int | None:
    """与 main._age_seconds_from_facebook_time_label 一致：相对时间越小表示越新。"""
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip().lower()
    s_oneline = re.sub(r"\s+", " ", s.split("\n")[0].strip())
    if re.search(r"just now|^now$|刚刚|片刻", s_oneline):
        return 0
    if "yesterday" in s or "昨天" in s:
        return int(36 * 3600)
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


@dataclass(frozen=True)
class PostItem:
    post_id: str
    url: str
    text: str
    detail: str = ""
    posted_at: str = ""
    posted_utime: int = 0
    # Lower = earlier in vertical feed after scroll-to-top (usually newer).
    feed_order: int = 999999


class FacebookPostMonitor:
    _shared_playwright = None
    _shared_browser = None
    _shared_context_users = 0
    _shared_persistent_context = None
    _persistent_context_users = 0

    def __init__(
        self,
        target_url: str,
        timeout_seconds: int = 10,
        cookie: str = "",
        accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8",
        browser_user_data_dir: str = "",
        browser_headless: bool = True,
        fb_email: str = "",
        fb_password: str = "",
        feed_visual_clip_height: int = 900,
        extra_feed_ocr_enabled: bool = False,
        extra_feed_ocr_lang: str = "eng+chi_sim",
        profile_feed_scroll_passes: int = 11,
    ) -> None:
        self.target_url = target_url
        self.timeout_seconds = timeout_seconds
        self.cookie = cookie
        self.accept_language = accept_language
        self.browser_user_data_dir = (browser_user_data_dir or "").strip()
        self.headless = browser_headless
        self._fb_email = (fb_email or "").strip()
        self._fb_password = (fb_password or "").strip()
        self._uses_persistent_context = False
        parsed_target = urlparse(target_url)
        self.is_marketplace_target = "/marketplace/" in target_url
        self.use_browser_mode = "facebook.com" in (parsed_target.netloc or "").lower()
        self._context = None
        self._page = None
        self.last_login_wall_hint = False
        self._last_feed_top_visual_hash: str = ""
        self._last_feed_top_ocr_text: str = ""
        self._feed_visual_clip_height = max(400, min(1600, int(feed_visual_clip_height or 900)))
        self._extra_feed_ocr_enabled = bool(extra_feed_ocr_enabled)
        self._extra_feed_ocr_lang = (extra_feed_ocr_lang or "eng").strip() or "eng"
        # 最近一次：主页主列首条可见动态的 DOM / OCR / 截图路径（不写死预期正文）。
        self.last_top_post_story_dom_text: str = ""
        self.last_top_post_story_ocr_text: str = ""
        self.last_top_post_story_post_url: str = ""
        self.last_top_post_story_screenshot_path: str = ""
        # 首条 story 截图 clip 的平均哈希（16 hex），用于与 OCR 文本解耦，避免同图 OCR 抖动误报「更新」
        self.last_top_post_story_clip_hash: str = ""
        # profile 模式下连续多次「截图+OCR」全部失败的标志：上层据此发「需要人工介入」告警
        self.last_screenshot_persistent_failure: bool = False
        # 时间线向下滚动采样次数（0=仅首屏/当前视口，不 scrollBy；用于与「只认 Posts 下首条」一致）
        self._profile_feed_scroll_passes = max(0, min(20, int(profile_feed_scroll_passes)))

    def _project_logs_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "logs"

    def _purge_other_fb_story_pngs(self, log_dir: Path, keep_path: Path) -> None:
        """只保留即将写入的那张：删掉同目录下其它 fb_latest_post_story*.png。"""
        if not log_dir.is_dir():
            return
        try:
            keep_key = keep_path.resolve()
        except OSError:
            keep_key = keep_path
        for f in log_dir.iterdir():
            if not f.is_file() or not fnmatch.fnmatch(f.name, "fb_latest_post_story*.png"):
                continue
            try:
                if f.resolve() != keep_key:
                    f.unlink()
            except OSError:
                pass

    def _persist_latest_fb_story_png(self, png: bytes, dest: Path | None = None) -> str:
        """写入最新一条 story 截图，并删除同目录旧版命名文件。"""
        out = dest or (self._project_logs_dir() / "fb_latest_post_story.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        self._purge_other_fb_story_pngs(out.parent, out)
        out.write_bytes(png)
        self.last_top_post_story_screenshot_path = str(out.resolve())
        return self.last_top_post_story_screenshot_path

    def _reset_top_story_state(self) -> None:
        """清空上一轮的首条 story 缓存。任何抓取/快照失败都必须调用，
        否则上层会把这一轮当成「OCR 和上次完全一致」从而错过新帖。"""
        self.last_top_post_story_dom_text = ""
        self.last_top_post_story_ocr_text = ""
        self.last_top_post_story_post_url = ""
        self.last_top_post_story_screenshot_path = ""
        self.last_top_post_story_clip_hash = ""

    def _passes_profile_allowlist(self, url: str) -> bool:
        # No per-profile hard allowlist: allow all detected post-like URLs.
        _ = url
        return True

    def _has_profile_allowlist(self) -> bool:
        return False

    def _target_profile_slug(self) -> str:
        if self.is_marketplace_target:
            return ""
        path = (urlparse(self.target_url).path or "").strip("/")
        if not path:
            return ""
        return path.split("/", 1)[0]

    def _is_in_target_scope(self, url: str) -> bool:
        if self.is_marketplace_target:
            return "/marketplace/item/" in url
        slug = self._target_profile_slug()
        if not slug:
            return False
        parsed = urlparse(url)
        path = parsed.path or ""
        return f"/{slug}/posts/" in path

    def _is_canonical_timeline_post_url(self, normalized: str) -> bool:
        """Only /{page}/posts/{id} — not story.php, /permalink/, photo, or comment query identities."""
        if self.is_marketplace_target:
            return True
        slug = self._target_profile_slug()
        if not slug:
            return False
        path = (urlparse(normalized).path or "").rstrip("/")
        m = re.match(r"^/([^/]+)/posts/([^/?#]+)$", path, re.I)
        if not m or m.group(1).lower() != slug.lower():
            return False
        rest = m.group(2)
        if not rest or "/" in rest:
            return False
        if rest.startswith("pfbid") and len(rest) >= 12:
            return True
        if rest.isdigit() and len(rest) >= 10:
            return True
        return bool(re.fullmatch(r"[A-Za-z0-9._-]{8,}", rest))

    def fetch_posts(self) -> list[PostItem]:
        if self.use_browser_mode:
            return self._fetch_posts_browser()

        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": self.accept_language,
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        response = requests.get(
            self.target_url,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        html = response.text
        return self._extract_posts(html, self.target_url)

    def close(self) -> None:
        cls = self.__class__
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None

        if self._uses_persistent_context:
            cls._persistent_context_users = max(0, cls._persistent_context_users - 1)
            self._context = None
            if cls._persistent_context_users == 0:
                try:
                    if cls._shared_persistent_context:
                        cls._shared_persistent_context.close()
                except Exception:
                    pass
                cls._shared_persistent_context = None
                try:
                    if cls._shared_playwright:
                        cls._shared_playwright.stop()
                except Exception:
                    pass
                cls._shared_playwright = None
            return

        had_context = self._context is not None
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if had_context:
            cls._shared_context_users = max(0, cls._shared_context_users - 1)
            if cls._shared_context_users == 0:
                try:
                    if cls._shared_browser:
                        cls._shared_browser.close()
                except Exception:
                    pass
                try:
                    if cls._shared_playwright:
                        cls._shared_playwright.stop()
                except Exception:
                    pass
                cls._shared_browser = None
                cls._shared_playwright = None

    def _finalize_browser_items(self, items: list[PostItem]) -> list[PostItem]:
        """When the page loads but we extract nothing, detect login/checkpoint pages for alerts."""
        if items:
            self.last_login_wall_hint = False
            return items
        self.last_login_wall_hint = False
        if not self._page:
            return items
        try:
            self.last_login_wall_hint = bool(
                self._page.evaluate(
                    """
                    () => {
                      try {
                        const u = (location.href || "").toLowerCase();
                        if (u.includes("facebook.com/login") || u.includes("/checkpoint")) return true;
                        if (document.querySelector('[data-testid="royal_login_form"], form#login_form')) return true;
                        if (document.querySelector('input#pass[type="password"], input[name="pass"][type="password"]'))
                          return true;
                        const t = ((document.body && document.body.innerText) || "").slice(0, 14000).toLowerCase();
                        if (t.includes("you must log in") && t.includes("continue")) return true;
                        if (t.includes("must log in to see")) return true;
                        if (t.includes("log in to facebook")) return true;
                        if (t.includes("see more on facebook") && t.includes("log in")) return true;
                        if (t.includes("登录") && t.includes("facebook")) return true;
                        if (t.includes("登入") && t.includes("facebook")) return true;
                        return false;
                      } catch (e) { return false; }
                    }
                    """
                )
            )
        except Exception:
            self.last_login_wall_hint = False
        return items

    _JS_IS_LOGIN_WALL = """
    () => {
      try {
        const u = (location.href || "").toLowerCase();
        if (u.includes("facebook.com/login") || u.includes("/checkpoint")) return true;
        if (document.querySelector('[data-testid="royal_login_form"], form#login_form')) return true;
        if (document.querySelector('input#pass[type="password"], input[name="pass"][type="password"]'))
          return true;
        const t = ((document.body && document.body.innerText) || "").slice(0, 14000).toLowerCase();
        if (t.includes("you must log in") && t.includes("continue")) return true;
        if (t.includes("must log in to see")) return true;
        if (t.includes("log in to facebook")) return true;
        if (t.includes("see more on facebook") && t.includes("log in")) return true;
        if (t.includes("登录") && t.includes("facebook")) return true;
        if (t.includes("登入") && t.includes("facebook")) return true;
        return false;
      } catch (e) { return false; }
    }
    """

    def _dismiss_post_login_interstitials(self) -> None:
        """登录成功后 FB 经常停留在「Save your login info」「Turn on notifications」
        「Trusted device」等遮罩页。不点掉的话后续 wait_for_selector(posts) 永远超时。
        只做最常见的「Not now / Cancel / Close / 暂时不要」点击，找不到就忽略。
        """
        if not self._page:
            return
        for sel in [
            'div[aria-label="Close"][role="button"]',
            'div[aria-label="关闭"][role="button"]',
            'div[role="button"]:has-text("Not now")',
            'div[role="button"]:has-text("Not Now")',
            'div[role="button"]:has-text("暂时不要")',
            'div[role="button"]:has-text("不用了")',
            'div[role="button"]:has-text("不允许")',
            'div[role="button"]:has-text("Don\'t allow")',
            'div[role="button"]:has-text("Save")',  # 罕见但偶尔需要点掉
            'a[role="button"]:has-text("Not now")',
            'a[role="button"]:has-text("暂时不要")',
        ]:
            try:
                btn = self._page.locator(sel).first
                if btn.is_visible(timeout=400):
                    btn.click(timeout=1500)
                    self._page.wait_for_timeout(500)
            except Exception:
                continue

    def _navigate_to_target_after_login(self) -> None:
        """登录返回后把页面带回真正的目标 URL；带最多 2 次重试。
        这是「自动登录成功 → 下一步 OCR 抓不到」的关键修复点。"""
        if not self._page:
            return
        target = self.target_url or ""
        if not target:
            return
        for attempt in range(2):
            try:
                self._page.goto(target, wait_until="domcontentloaded", timeout=20000)
                self._page.wait_for_timeout(1500)
                self._dismiss_post_login_interstitials()
                # 撞回登录墙 → 再登一次（极少见）；否则跳出
                try:
                    if not self._page.evaluate(self._JS_IS_LOGIN_WALL):
                        return
                except Exception:
                    return
            except Exception:
                if attempt == 0:
                    self._page.wait_for_timeout(1500)
                    continue
                return

    def _attempt_auto_login(self) -> bool:
        """Detect login wall and auto-fill credentials. Returns True if login succeeded.

        登录返回前会自动：清理 cookie banner、关闭 "save info / notifications" 弹窗，
        并把页面 goto 回 ``self.target_url``，保证下游 fetch_posts / 视口截图拿到的是真正的主页。
        """
        if not self._page or not self._fb_email or not self._fb_password:
            return False
        try:
            if not self._page.evaluate(self._JS_IS_LOGIN_WALL):
                return False
        except Exception:
            return False
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] auto-login: login wall detected, attempting login with {self._fb_email[:4]}***", flush=True)

        def _save_debug_screenshot(tag: str) -> None:
            try:
                p = self._project_logs_dir() / f"fb_auto_login_{tag}.png"
                self._page.screenshot(path=str(p), type="png")
                print(f"[{ts}] auto-login: debug screenshot → {p}", flush=True)
            except Exception:
                pass

        def _dismiss_cookie_banner() -> None:
            for sel in [
                'button[data-cookiebanner="accept_button"]',
                'button[data-testid="cookie-policy-manage-dialog-accept-button"]',
                'button[title="Allow all cookies"]',
                'button[title="允许所有 Cookie"]',
                'button[title="Accept All"]',
                '[aria-label="Allow all cookies"]',
                '[aria-label="Accept All"]',
            ]:
                try:
                    btn = self._page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        self._page.wait_for_timeout(1000)
                        return
                except Exception:
                    continue

        def _dismiss_checkpoint_warning() -> bool:
            """Handle FB 'We suspect automated behavior' or similar checkpoint warnings.
            Returns True if a warning was dismissed and login is now OK."""
            try:
                body = (self._page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 5000)"
                ) or "").lower()
            except Exception:
                return False
            checkpoint_phrases = [
                "suspect automated behavior",
                "suspicious activity",
                "we suspect automated",
                "检测到自动行为",
                "可疑活动",
            ]
            if not any(p in body for p in checkpoint_phrases):
                return False
            print(f"[{ts}] auto-login: checkpoint warning detected, attempting to dismiss", flush=True)
            for sel in [
                'button:has-text("Dismiss")',
                'button:has-text("关闭")',
                'button:has-text("OK")',
                'button:has-text("确定")',
                'a:has-text("Dismiss")',
                'div[role="button"]:has-text("Dismiss")',
                'div[role="button"]:has-text("关闭")',
            ]:
                try:
                    btn = self._page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        self._page.wait_for_load_state("domcontentloaded", timeout=15000)
                        self._page.wait_for_timeout(3000)
                        still = False
                        try:
                            still = bool(self._page.evaluate(self._JS_IS_LOGIN_WALL))
                        except Exception:
                            pass
                        if not still:
                            print(f"[{ts}] auto-login: checkpoint dismissed successfully", flush=True)
                            return True
                        print(f"[{ts}] auto-login: checkpoint dismissed but still on login-like page", flush=True)
                        return False
                except Exception:
                    continue
            _save_debug_screenshot("checkpoint_no_dismiss_btn")
            print(f"[{ts}] auto-login: checkpoint warning found but no dismiss button", flush=True)
            return False

        try:
            self._page.goto(
                "https://www.facebook.com/login/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            self._page.wait_for_timeout(3000)
            _dismiss_cookie_banner()

            # "Continue as [name]" page → click Continue → then fill password
            continue_clicked = False
            for cont_sel in [
                'div[role="button"]:has-text("继续")',
                'div[role="button"]:has-text("Continue")',
                'span:has-text("继续")',
                'span:has-text("Continue")',
            ]:
                try:
                    el = self._page.locator(cont_sel).first
                    if el.is_visible(timeout=1000):
                        print(f"[{ts}] auto-login: clicking 'Continue as' button", flush=True)
                        el.click()
                        self._page.wait_for_load_state("domcontentloaded", timeout=30000)
                        self._page.wait_for_timeout(3000)
                        continue_clicked = True
                        break
                except Exception:
                    continue
            if continue_clicked:
                still_login = False
                try:
                    still_login = bool(self._page.evaluate(self._JS_IS_LOGIN_WALL))
                except Exception:
                    pass
                if not still_login:
                    print(f"[{ts}] auto-login: login succeeded (one-click)", flush=True)
                    self._dismiss_post_login_interstitials()
                    self._navigate_to_target_after_login()
                    return True
                # Password page after Continue
                pass_el = None
                for sel in ['input#pass', 'input[name="pass"]', 'input[type="password"]']:
                    try:
                        el = self._page.query_selector(sel)
                        if el and el.is_visible():
                            pass_el = el
                            break
                    except Exception:
                        continue
                if pass_el:
                    print(f"[{ts}] auto-login: password page detected, filling password", flush=True)
                    pass_el.click()
                    pass_el.fill(self._fb_password)
                    submitted = False
                    for btn_sel in [
                        'button[name="login"]',
                        'button[type="submit"]',
                        'button:has-text("登录")',
                        'button:has-text("Log In")',
                        'button:has-text("Continue")',
                        'button:has-text("继续")',
                    ]:
                        try:
                            btn = self._page.locator(btn_sel).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                submitted = True
                                break
                        except Exception:
                            continue
                    if not submitted:
                        pass_el.press("Enter")
                    self._page.wait_for_load_state("domcontentloaded", timeout=30000)
                    self._page.wait_for_timeout(8000)
                    if _dismiss_checkpoint_warning():
                        print(f"[{ts}] auto-login: login succeeded (continue + password + dismiss checkpoint)", flush=True)
                        self._dismiss_post_login_interstitials()
                        self._navigate_to_target_after_login()
                        return True
                    still2 = False
                    try:
                        still2 = bool(self._page.evaluate(self._JS_IS_LOGIN_WALL))
                    except Exception:
                        pass
                    if not still2:
                        print(f"[{ts}] auto-login: login succeeded (continue + password)", flush=True)
                        self._dismiss_post_login_interstitials()
                        self._navigate_to_target_after_login()
                        return True
                    # 按钮可能还在 loading（网络慢），再等一次
                    self._page.wait_for_timeout(10000)
                    still3 = False
                    try:
                        still3 = bool(self._page.evaluate(self._JS_IS_LOGIN_WALL))
                    except Exception:
                        pass
                    if not still3:
                        print(f"[{ts}] auto-login: login succeeded (continue + password, delayed)", flush=True)
                        self._dismiss_post_login_interstitials()
                        self._navigate_to_target_after_login()
                        return True
                    _save_debug_screenshot("after_password")
                    print(f"[{ts}] auto-login: still on login page after password submit", flush=True)
                    return False
                _save_debug_screenshot("continue_no_pass")
                print(f"[{ts}] auto-login: clicked Continue but no password field found", flush=True)
                return False

            email_selectors = [
                'input#email',
                'input[name="email"]',
                'input[type="email"]',
                'input[aria-label="Email address or phone number"]',
                'input[aria-label="电子邮箱或手机号"]',
            ]
            email_el = None
            for sel in email_selectors:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        email_el = el
                        break
                except Exception:
                    continue

            if not email_el:
                _save_debug_screenshot("no_email_field")
                page_url = self._page.url or ""
                print(f"[{ts}] auto-login: no email field found on {page_url}", flush=True)
                body_text = ""
                try:
                    body_text = (self._page.evaluate(
                        "() => (document.body.innerText || '').slice(0, 2000)"
                    ) or "")
                except Exception:
                    pass
                print(f"[{ts}] auto-login: page text preview: {body_text[:300]}", flush=True)
                return False

            email_el.click()
            email_el.fill(self._fb_email)
            pass_el = None
            for sel in ['input#pass', 'input[name="pass"]', 'input[type="password"]']:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        pass_el = el
                        break
                except Exception:
                    continue
            if not pass_el:
                _save_debug_screenshot("no_pass_field")
                print(f"[{ts}] auto-login: no password field found", flush=True)
                return False

            pass_el.click()
            pass_el.fill(self._fb_password)

            login_clicked = False
            for sel in [
                'button[name="login"]',
                'button[data-testid="royal_login_button"]',
                'button[type="submit"]',
                'input[type="submit"]',
                '#loginbutton',
            ]:
                try:
                    btn = self._page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        login_clicked = True
                        break
                except Exception:
                    continue

            if not login_clicked:
                pass_el.press("Enter")

            self._page.wait_for_load_state("domcontentloaded", timeout=30000)
            self._page.wait_for_timeout(5000)

            if _dismiss_checkpoint_warning():
                print(f"[{ts}] auto-login: login succeeded (email + password + dismiss checkpoint)", flush=True)
                self._dismiss_post_login_interstitials()
                self._navigate_to_target_after_login()
                return True

            still_login = False
            try:
                still_login = bool(self._page.evaluate(self._JS_IS_LOGIN_WALL))
            except Exception:
                pass
            if still_login:
                _save_debug_screenshot("still_login")
                page_text = ""
                try:
                    page_text = (self._page.evaluate(
                        "() => (document.body.innerText || '').slice(0, 3000)"
                    ) or "").lower()
                except Exception:
                    pass
                if any(kw in page_text for kw in [
                    "two-factor", "two factor", "验证码",
                    "approval code", "code generator", "check your email",
                    "we sent a code", "enter the code",
                ]):
                    print(f"[{ts}] auto-login: 2FA/verification required — manual intervention needed", flush=True)
                else:
                    print(f"[{ts}] auto-login: login may have failed (still on login page)", flush=True)
                return False
            print(f"[{ts}] auto-login: login succeeded", flush=True)
            self._dismiss_post_login_interstitials()
            self._navigate_to_target_after_login()
            return True
        except Exception as exc:
            _save_debug_screenshot("exception")
            print(f"[{ts}] auto-login: failed — {exc}", flush=True)
            return False

    def _posts_tab_url(self) -> str | None:
        """主页根 URL 时尝试 /{slug}/posts，帖子更密集、更易按时间排序。"""
        if self.is_marketplace_target:
            return None
        path = (urlparse(self.target_url).path or "").strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 1:
            return None
        slug = parts[0]
        if slug.lower() in {"watch", "marketplace", "groups", "reel", "reels", "gaming"}:
            return None
        tab = f"https://www.facebook.com/{slug}/posts"
        if self.target_url.rstrip("/").lower() == tab.lower():
            return None
        return tab

    @staticmethod
    def _merge_post_items_by_url(a: list[PostItem], b: list[PostItem]) -> list[PostItem]:
        now = int(time.time())
        recent_cut = now - 7 * 86400
        pl_min, pl_max = 1_000_000, 220_000_000

        def prefer_post(x: PostItem, y: PostItem) -> PostItem:
            ux, uy = x.posted_utime or 0, y.posted_utime or 0
            if ux != uy:
                return x if ux > uy else y
            fx, fy = int(x.feed_order or 0), int(y.feed_order or 0)
            if pl_min <= fx < pl_max and pl_min <= fy < pl_max:
                return x if fx < fy else y
            if pl_min <= fx < pl_max:
                return x
            if pl_min <= fy < pl_max:
                return y
            return x if x.feed_order < y.feed_order else y

        merged: dict[str, PostItem] = {}
        for p in a + b:
            u = p.url
            old = merged.get(u)
            merged[u] = p if old is None else prefer_post(p, old)
        out = list(merged.values())

        def sort_key(x: PostItem) -> tuple:
            u = x.posted_utime or 0
            if u >= recent_cut:
                return (0, -u, 0, x.feed_order)
            fe = int(x.feed_order or 0)
            if pl_min <= fe < pl_max:
                return (1, fe, 0, 0)
            la = _fb_label_age_seconds(x.posted_at)
            if la is not None:
                return (2, la, 0, x.feed_order)
            return (3, 999999999, x.feed_order, -u)

        out.sort(key=sort_key)
        return out

    @staticmethod
    def _feed_body_substance_score(text: str) -> int:
        """Higher = more likely real post body（不把文中出现的 reaction 子串当成整段无效）。"""
        t = (text or "").strip()
        if len(t) < 6:
            return 0
        low = t.lower()
        if FacebookPostMonitor._is_reaction_placeholder_body(t):
            return min(len(t), 20)
        if re.match(r"^\d+\s+reactions?\b", low) and len(t) < 100:
            return min(len(t), 28)
        if re.fullmatch(r"what'?s the price\??", low):
            return min(len(t), 24)
        return min(len(t), 800)

    @staticmethod
    def _is_reaction_placeholder_body(text: str) -> bool:
        """纯点赞/心情列表等，不是帖子正文。"""
        t = (text or "").strip().lower()
        if len(t) < 8:
            return False
        if t.startswith("see who reacted"):
            return True
        if re.match(r"^\d+\s+reactions?\b", t) and "see who reacted" in t:
            return True
        if re.match(r"^\d+\s+reactions?\b", t) and len(t) < 90:
            return True
        return False

    @staticmethod
    def _is_noise_post_time_label(label: str) -> bool:
        """时间文案若来自 reaction/评论区则丢弃，避免干扰排序。"""
        x = re.sub(r"\s+", " ", (label or "").strip().lower())
        if not x:
            return False
        return bool(
            re.search(
                r"reaction|see who reacted|view\s+\d+\s+comments|条评论|所有心情|commented on",
                x,
            )
        )

    @staticmethod
    def _timeline_body_token_count(text: str) -> int:
        """≥2 个字母/数字组成的 token 数（粗略区分「一句评论」与段落帖）。"""
        return len(
            [
                w
                for w in re.split(r"\s+", (text or "").strip())
                if len(re.sub(r"[^\w\u00C0-\u024f]", "", w, flags=re.I)) >= 2
            ]
        )

    @staticmethod
    def _is_plausible_timeline_post_body(text: str) -> bool:
        """通用：像主页动态正文，而非纯 reaction 条。长文即使夹带互动文案也保留。"""
        t = (text or "").strip()
        if len(t) < 14:
            return False
        if FacebookPostMonitor._is_reaction_placeholder_body(t):
            return False
        ntok = FacebookPostMonitor._timeline_body_token_count(t)
        if ntok < 3:
            return False
        if len(t) >= 88 and ntok >= 4:
            return True
        sub = FacebookPostMonitor._feed_body_substance_score(t)
        if sub <= 0:
            return False
        if sub >= 32:
            return True
        if len(t) >= 48 and sub >= 22:
            return True
        low = t.lower()
        if len(t) >= 18 and (
            "see more" in low
            or "查看更多" in t
            or "顯示更多" in t
            or "ver más" in low
        ):
            return True
        if len(t) >= 24 and ("…" in t or "..." in t) and sub >= 18:
            return True
        if ntok >= 4 and len(t) >= 16:
            return True
        return len(t) >= 72

    _JS_PERMALINK_POST_BODY = """
    (postFrag) => {
      const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
      const frag = String(postFrag || "").trim();
      if (!frag) return "";
      const sels = [
        "[data-ad-preview='message']",
        "[data-ad-rendering-role='story_message']",
        "[data-testid='post_message']",
        "[data-ad-comet-preview='message']",
      ];
      const arts = Array.from(document.querySelectorAll('div[role="article"]'));
      for (const art of arts) {
        let hit = false;
        for (const a of art.querySelectorAll('a[href*="/posts/"]')) {
          const h = a.getAttribute("href") || "";
          if (h.includes(frag)) {
            hit = true;
            break;
          }
        }
        if (!hit) continue;
        for (const sel of sels) {
          const n = art.querySelector(sel);
          if (n) {
            const t = clean(n.innerText || n.textContent || "");
            if (t.length > 14) return t.slice(0, 900);
          }
        }
      }
      return "";
    }
    """

    _JS_FOCUS_POSTS_SECTION_HEADER = """
    () => {
      /** 先归零，再把 Posts+Filters 行滚到视口上半部（页顶壳子通常还没到帖子）。 */
      try {
        window.scrollTo(0, 0);
      } catch (e0) {}
      const main =
        document.querySelector('[role="main"]') ||
        document.querySelector('[data-pagelet*="MainColumn"]') ||
        document.body;
      const vh = window.innerHeight || 900;
      const feed = main.querySelector('[role="feed"]');
      if (feed) {
        try {
          feed.scrollTop = 0;
        } catch (e1) {}
        let n = feed;
        for (let i = 0; i < 6 && n; i++) {
          try {
            if (n.scrollHeight > n.clientHeight + 80) n.scrollTop = 0;
          } catch (e2) {}
          n = n.parentElement;
        }
      }
      let postsTop = null;
      for (const row of main.querySelectorAll("div, section")) {
        const tx = ((row.innerText || "") + "").slice(0, 320).replace(/\\s+/g, " ");
        if (!/\\bPosts\\b/.test(tx) || !/filter/i.test(tx)) continue;
        const r = row.getBoundingClientRect();
        if (r.width < 160 || r.height < 14 || r.height > 480) continue;
        if (r.top < -300 || r.top > vh + 240) continue;
        if (postsTop === null || r.top < postsTop) postsTop = r.top;
      }
      if (postsTop !== null) {
        const dy = Math.round(postsTop - Math.max(96, Math.floor(vh * 0.16)));
        if (Math.abs(dy) > 12) {
          try {
            window.scrollBy(0, dy);
          } catch (e3) {}
        }
      } else {
        /** 未命中 Posts 行时轻微下滚，避免只停在主页封面壳子 */
        try {
          window.scrollBy(0, Math.floor(vh * 0.34));
        } catch (e4) {}
      }
      return postsTop !== null ? "posts_anchor" : feed ? "feed_reset" : "no_feed";
    }
    """

    _JS_FIRST_VISIBLE_STORY_SNAPSHOT = """
    (profileSlug) => {
      const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
      const slug = String(profileSlug || "").trim();
      if (!slug) return { clip: null, domText: "", postUrl: "" };
      const needle = "/" + slug + "/posts/";
      const vw0 = window.innerWidth || 1280;
      const vh0 = window.innerHeight || 900;
      const main =
        document.querySelector('[role="main"]') ||
        document.querySelector('[data-pagelet*="MainColumn"]') ||
        document.querySelector("#mount_0_0") ||
        document.body;
      let postsCutY = -1e9;
      let postsHeaderTop = -1e9;
      let postsHeaderBottom = -1e9;
      let postsHeaderLeft = 0;
      let postsHeaderRight = vw0;
      for (const row of main.querySelectorAll("div, section")) {
        const tx = ((row.innerText || "") + "").slice(0, 320).replace(/\\s+/g, " ");
        if (!/\\bPosts\\b/.test(tx) || !/filter/i.test(tx)) continue;
        const r = row.getBoundingClientRect();
        if (r.width < 160 || r.height < 14 || r.height > 480) continue;
        if (r.top < -220 || r.top > vh0 + 120) continue;
        /** 取几何位置最靠上的 Posts+Filters 行（截图锚点），避免 max(bottom) 选到下面重复条 */
        if (postsHeaderTop < -1e8 + 1 || r.top < postsHeaderTop - 1) {
          postsHeaderTop = r.top;
          postsHeaderBottom = r.bottom;
          postsCutY = r.bottom;
          postsHeaderLeft = r.left;
          postsHeaderRight = r.right;
        }
      }
      if (postsCutY < -1e8 + 1) {
        const feed = main.querySelector('[role="feed"]');
        if (feed) {
          const fr = feed.getBoundingClientRect();
          postsCutY = fr.top + 2;
        }
      }
      const feed = main.querySelector('[role="feed"]');
      if (feed) {
        try {
          feed.scrollTop = 0;
        } catch (e1) {}
      }
      const scope = feed || main;
      const pad = 8;
      const vw = vw0;
      const vh = vh0;
      const isCueRetailStory = (t) => {
        const s = String(t || "").toLowerCase();
        return /\\b(cue|shaft|mezz|pool|billiard|sale|sold|see more|在售|出售|ast[rz])\\b/i.test(s);
      };
      /** 常见「Sigma 升级问答」占位卡：无球杆帖特征，易排在帖子上方误导排序 */
      const looksLikeSigmaFaqCard = (t) => {
        const s = String(t || "");
        if (s.length > 420) return false;
        if (!/sigma\\s+slim\\s+upgrade/i.test(s)) return false;
        return !isCueRetailStory(s);
      };
      const hasFbTimeLike = (el, domText) => {
        try {
          if (el.querySelector("time[datetime], time[title], abbr[data-utime]")) return true;
        } catch (e4) {}
        const raw = (el.innerText || "").slice(0, 900);
        if (
          /\\b(yesterday|just now|\\d+\\s*h\\b|\\d+\\s*m\\b|\\d+\\s*d\\b|\\d+\\s*w\\b)\\b/i.test(
            raw,
          ) ||
          /\\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\s+\\d{1,2}\\b/i.test(raw)
        )
          return true;
        return isCueRetailStory(domText);
      };
      /**
       * Comet 虚拟列表：同一 story 外壳下多个兄弟 div，占位格带 aria-hidden="true"。
       * 不能用 querySelector 取「第一个 message」，否则会读到隐藏槽里的旧 DOM。
       */
      const isHiddenWithinArticle = (node, articleRoot) => {
        let n = node;
        for (let i = 0; i < 48 && n; i++) {
          if (n === articleRoot) return false;
          try {
            if (
              n.nodeType === 1 &&
              n.getAttribute &&
              n.getAttribute("aria-hidden") === "true"
            )
              return true;
          } catch (eH) {}
          n = n.parentElement;
        }
        return false;
      };
      const hasUsableRect = (el) => {
        try {
          const br = el.getBoundingClientRect();
          if (br.width < 18 || br.height < 6) return false;
          /** 略放宽：仅排除明显在视口外很远的节点（避免误杀主列内正文） */
          if (br.bottom < -160 || br.top > vh + 220) return false;
          const cs = window.getComputedStyle(el);
          if (cs.display === "none" || cs.visibility === "hidden") return false;
          const op = parseFloat(cs.opacity || "1");
          if (op < 0.04) return false;
        } catch (eR) {
          return false;
        }
        return true;
      };
      /**
       * 返回 [node, strict]：strict=true 表示未落在 aria-hidden 链上，可同时对 /posts/ 链做隐藏过滤。
       * FB 偶发把主列误标 aria-hidden，strict 全灭时回退为「可见矩形里面积最大的 message」。
       * scopeRoot：只在可见虚拟槽内搜 message；chainRoot：向上判 aria-hidden 时止于此 article。
       */
      const pickVisibleMessage = (scopeRoot, chainRoot) => {
        const sels = [
          "[data-ad-preview='message']",
          "[data-ad-rendering-role='story_message']",
          "[data-testid='post_message']",
          "[data-ad-comet-preview='message']",
        ];
        const cand = [];
        const seen = new Set();
        for (const sel of sels) {
          scopeRoot.querySelectorAll(sel).forEach((n) => {
            if (seen.has(n)) return;
            seen.add(n);
            cand.push(n);
          });
        }
        const validCands = [];
        for (const n of cand) {
          if (isHiddenWithinArticle(n, chainRoot)) continue;
          if (!hasUsableRect(n)) continue;
          validCands.push(n);
        }
        if (validCands.length > 0) {
          return [validCands[0], true];
        }
        let best = null;
        let bestArea = -1;
        for (const n of cand) {
          if (!hasUsableRect(n)) continue;
          try {
            const br = n.getBoundingClientRect();
            if (vw >= 900 && br.left < vw * 0.14) continue;
            const ar = br.width * br.height;
            if (ar > bestArea) {
              bestArea = ar;
              best = n;
            }
          } catch (eA2) {}
        }
        return best ? [best, false] : [null, false];
      };
      /**
       * 你 F12 里那种结构：同一父下多个 div 兄弟，若干 aria-hidden 占位 + 一格无 aria-hidden 为当前帖。
       * 在该「可见槽」内再取正文/链接，避免整颗 article 里 query 到占位格旧 DOM。
       */
      const findVirtualizedLiveSlot = (art, needle) => {
        const postNeedle = String(needle || "").replace(/"/g, "");
        let budget = 0;
        const q = [art];
        const ahTrue = (c) => c.getAttribute && c.getAttribute("aria-hidden") === "true";
        while (q.length && budget < 520) {
          budget++;
          const root = q.shift();
          if (!root || !root.children || root.nodeType !== 1) continue;
          const kids = Array.from(root.children).filter(
            (c) => c && c.nodeType === 1 && String(c.tagName).toLowerCase() === "div",
          );
          if (kids.length >= 2) {
            let nHidden = 0;
            const shown = [];
            for (const c of kids) {
              if (ahTrue(c)) nHidden++;
              else {
                try {
                  const br = c.getBoundingClientRect();
                  if (
                    br.width > 88 &&
                    br.height > 26 &&
                    br.bottom > -90 &&
                    br.top < vh + 120
                  )
                    shown.push(c);
                } catch (e) {}
              }
            }
            if (nHidden >= 1 && shown.length >= 1) {
              let best = null;
              let bestScore = -1;
              for (const slot of shown) {
                let score = 0;
                try {
                  const br = slot.getBoundingClientRect();
                  score = br.width * br.height;
                } catch (e) {}
                const hasMsg = !!(
                  slot.querySelector("[data-ad-preview='message']") ||
                  slot.querySelector("[data-ad-rendering-role='story_message']") ||
                  slot.querySelector("[data-testid='post_message']") ||
                  slot.querySelector("[data-ad-comet-preview='message']")
                );
                let postHit = false;
                try {
                  slot.querySelectorAll("a[href]").forEach((a) => {
                    const h = a.getAttribute("href") || "";
                    if (postNeedle && h.includes(postNeedle) && /\\/posts\\//i.test(h))
                      postHit = true;
                  });
                } catch (e2) {}
                const tl = clean(slot.innerText || "").length;
                if (hasMsg) score += 3e9;
                if (postHit) score += 2e9;
                score += Math.min(tl * 200, 4e8);
                if (score > bestScore) {
                  bestScore = score;
                  best = slot;
                }
              }
              if (best) return best;
            }
          }
          for (const c of kids) q.push(c);
        }
        return null;
      };
      const arts = Array.from(scope.querySelectorAll('div[role="article"]'));
      /** 先找虚拟列表「可见槽」，再在槽内取 message（对齐 F12 红框 div 所在那一格） */
      for (const art of arts) {
        try {
          if (art.getAttribute && art.getAttribute("aria-hidden") === "true") continue;
        } catch (eA) {}
        /** Messenger 弹窗的 article 不在 feed 内，强制排除避免把聊天内容误认为帖子 */
        if (feed && !feed.contains(art)) continue;
        try {
          const bra = art.getBoundingClientRect();
          if (bra.width < 32 || bra.height < 18) continue;
        } catch (eB) {
          continue;
        }
        const storyRoot = findVirtualizedLiveSlot(art, needle) || art;
        const picked = pickVisibleMessage(storyRoot, art);
        const msg = picked[0];
        const linkAriaStrict = picked[1];
        if (!msg) continue;
        const r = msg.getBoundingClientRect();
        if (r.width < 36 || r.height < 14) continue;
        if (postsCutY > -1e8 + 1 && r.top < postsCutY - 10) continue;
        if (vw >= 900 && r.left < vw * 0.19) continue;
        const domText = clean(msg.innerText || msg.textContent || "");
        if (domText.length < 10) continue;
        if (looksLikeSigmaFaqCard(domText)) continue;
        if (!hasFbTimeLike(storyRoot, domText)) continue;
        const seenH = new Set();
        const hrefsPfbid = [];
        const hrefsOther = [];
        for (const a of storyRoot.querySelectorAll("a[href]")) {
          if (linkAriaStrict && isHiddenWithinArticle(a, art)) continue;
          const h = a.getAttribute("href") || "";
          if (!h.includes(needle) || !/\\/posts\\//i.test(h)) continue;
          try {
            const u = new URL(h, location.href);
            const pn = (u.pathname || "").replace(/\\/+$/, "");
            const full = "https://www.facebook.com" + pn;
            if (seenH.has(full)) continue;
            seenH.add(full);
            if (/\\/posts\\/pfbid/i.test(full)) hrefsPfbid.push(full);
            else hrefsOther.push(full);
          } catch (e5) {}
        }
        const postHref = hrefsPfbid[0] || hrefsOther[0] || "";
        if (!postHref) continue;
        let storyBottom = r.bottom;
        try {
          const sr = storyRoot.getBoundingClientRect();
          if (sr && sr.height > 20) storyBottom = Math.max(storyBottom, sr.bottom);
        } catch (e3) {}
        try {
          const ar = art.getBoundingClientRect();
          if (ar && ar.height > 20) storyBottom = Math.max(storyBottom, ar.bottom);
        } catch (e3b) {}
        let colLeft = r.left;
        if (postsHeaderLeft > 10 && postsHeaderLeft < colLeft) colLeft = postsHeaderLeft;
        const msgTop = r.top;
        /** 只截文字区（msg 元素底部 + pad），不延伸到文章底部（图片/评论区），
         *  避免 OCR 读取图片上的文字（如 GENTEI COLLECTION / MGC-26）污染正文。*/
        const msgBottom = r.bottom > r.top ? r.bottom : r.top + r.height;
        let y0;
        let y1;
        const FB_NAV_H = 56; // Facebook 顶部固定导航栏高度
        if (postsHeaderTop > -1e8 + 1) {
          y0 = Math.max(FB_NAV_H, Math.floor(Math.min(postsHeaderTop, msgTop) - pad));
          y1 = Math.min(vh, Math.ceil(Math.max(postsHeaderBottom, msgBottom) + pad));
        } else {
          y0 = Math.max(FB_NAV_H, Math.floor(msgTop - pad));
          y1 = Math.min(vh, Math.ceil(msgBottom + pad));
        }
        const hClip = Math.max(120, Math.min(y1 - y0, Math.floor(vh * 0.94)));
        if (hClip < 80) continue;
        return {
          clip: { x: 0, y: y0, width: vw, height: hClip },
          postsColumnLeft: Math.max(0, Math.floor(colLeft)),
          domText,
          postUrl: postHref,
        };
      }
      return { clip: null, domText: "", postUrl: "", postsColumnLeft: Math.max(0, Math.floor(postsHeaderLeft)) };
    }
    """

    def _permalink_post_id_fragment(self, post_url: str) -> str:
        m = re.search(r"/posts/([^/?#]+)", post_url or "", re.I)
        return (m.group(1) or "").strip() if m else ""

    def _enrich_short_post_bodies_from_permalink(self, items: list[PostItem]) -> list[PostItem]:
        """时间线正文被虚拟化时打开 permalink：Comet 页常无 og:description，在含该帖链接的 story article 内取正文。"""
        if not items or not self._page or self.is_marketplace_target:
            return items
        enrich_limit = 1 if int(getattr(self, "_profile_feed_scroll_passes", 11)) == 0 else 20
        out: list[PostItem] = []
        for i, p in enumerate(items):
            tx = (p.text or "").strip()
            if len(tx) >= 22 or i >= enrich_limit:
                out.append(p)
                continue
            try:
                self._page.goto(p.url, wait_until="domcontentloaded", timeout=20000)
                self._page.wait_for_timeout(2200)
                frag = self._permalink_post_id_fragment(p.url)
                msg = (self._page.evaluate(self._JS_PERMALINK_POST_BODY, frag) or "").strip()
                if len(msg) < 15:
                    html = self._page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    og = soup.find("meta", property="og:description")
                    if og and (og.get("content") or "").strip():
                        msg = (og.get("content") or "").strip()
                    if len(msg) < 12:
                        nm = soup.find("meta", attrs={"name": "description"})
                        if nm and (nm.get("content") or "").strip():
                            msg = (nm.get("content") or "").strip()
                if len(msg) > 14:
                    out.append(replace(p, text=msg[:700]))
                else:
                    out.append(p)
            except Exception:
                out.append(p)
        out = sorted(out, key=lambda x: int(x.feed_order or 10**9))
        groups: dict[str, list] = defaultdict(list)
        for p in out:
            tx = (p.text or "").strip()
            key = re.sub(r"\s+", " ", tx.lower())[:500] if tx else ""
            groups[key].append(p)
        deduped: list[PostItem] = []
        for key, lst in groups.items():
            if not key:
                deduped.extend(lst)
            elif len(lst) == 1:
                deduped.append(lst[0])
            else:
                deduped.append(min(lst, key=lambda x: int(x.feed_order or 10**9)))
        return sorted(deduped, key=lambda x: int(x.feed_order or 10**9))

    def _finalize_profile_timeline_items(self, items: list[PostItem]) -> list[PostItem]:
        """同帖 URL 去重；去掉 reaction 占位；清掉来自互动区的时间文案；去掉明显非正文短句。"""
        if not items:
            return items
        merged: dict[str, PostItem] = {}
        for p in items:
            u = self._normalize_url(p.url)
            merged[u] = p if u not in merged else self._prefer_post_item_merge(merged[u], p)
        def one_pass(min_body_len: int, use_plausible: bool) -> list[PostItem]:
            acc: list[PostItem] = []
            for p in merged.values():
                if self._is_reaction_placeholder_body(p.text or ""):
                    continue
                tx = (p.text or "").strip()
                if len(tx) < min_body_len:
                    continue
                if use_plausible and tx and not self._is_plausible_timeline_post_body(tx):
                    continue
                if self._is_noise_post_time_label(p.posted_at or ""):
                    p = replace(p, posted_at="")
                acc.append(p)
            return acc

        out = one_pass(16, True)
        if not out:
            out = one_pass(0, False)
        return out

    @staticmethod
    def _prefer_post_item_merge(a: PostItem, b: PostItem) -> PostItem:
        """同 URL：实质正文 / utime 更新 / 相对时间更短 优先。"""
        sa = FacebookPostMonitor._feed_body_substance_score(a.text or "")
        sb = FacebookPostMonitor._feed_body_substance_score(b.text or "")
        if sb > sa + 5:
            return replace(b, feed_order=min(a.feed_order, b.feed_order))
        if sa > sb + 5:
            return replace(a, feed_order=min(a.feed_order, b.feed_order))
        la, lb = len((a.text or "").strip()), len((b.text or "").strip())
        if lb > la + 8:
            return replace(b, feed_order=min(a.feed_order, b.feed_order))
        if la > lb + 8:
            return replace(a, feed_order=min(a.feed_order, b.feed_order))
        ua, ub = a.posted_utime or 0, b.posted_utime or 0
        if ua != ub:
            return a if ua > ub else b
        aa = _fb_label_age_seconds(a.posted_at)
        bb = _fb_label_age_seconds(b.posted_at)
        if aa is not None and bb is not None and aa != bb:
            return a if aa < bb else b
        if aa is not None and bb is None:
            return a
        if bb is not None and aa is None:
            return b
        return a if a.feed_order <= b.feed_order else b

    def _profile_post_url_first_positions(self, html: str) -> dict[str, int]:
        """URL → 在 HTML 中首次出现位置（越小通常越靠近当前页序列头部）。"""
        slug = self._target_profile_slug()
        if not slug or not html or self.is_marketplace_target:
            return {}
        first_pos: dict[str, int] = {}
        esc = re.escape(slug)
        for pat in (
            rf'https?://(?:www\.)?facebook\.com/{esc}/posts/(pfbid[a-zA-Z0-9]+|\d{{10,}})',
            rf'https?:\\/\\/(?:www\\.)?facebook\\.com\\/{esc}\\/posts\\/(pfbid[a-zA-Z0-9]+|\d{{10,}})',
        ):
            for m in re.finditer(pat, html, flags=re.I):
                pid = (m.group(1) or "").replace("\\/", "/")
                if not pid or "/" in pid or "\\" in pid:
                    continue
                url = self._normalize_url(f"https://www.facebook.com/{slug}/posts/{pid}")
                if not self._is_canonical_timeline_post_url(url):
                    continue
                pos = m.start()
                if url not in first_pos or pos < first_pos[url]:
                    first_pos[url] = pos
        return first_pos

    def _serialized_html_post_probe(self, html: str) -> list[PostItem]:
        """从整页 HTML/内嵌 JSON（含 \\/ 转义）里扫 /{slug}/posts/…，补 DOM 漏掉的帖链接。"""
        first_pos = self._profile_post_url_first_positions(html)
        if not first_pos:
            return []
        sorted_urls = sorted(first_pos.keys(), key=lambda u: first_pos[u])[:60]
        return [
            self._to_post_item(
                u,
                "",
                feed_order=1_000_000 + min(int(first_pos[u]) + i, 8_999_999),
            )
            for i, u in enumerate(sorted_urls)
        ]

    def _reindex_items_by_html_stream_order(self, items: list[PostItem], html: str) -> list[PostItem]:
        """按整页 HTML 中帖 URL 首次出现顺序重赋 feed_order，统一 DOM 与 JSON 探针的先后。"""
        pl_min, pl_max = 1_000_000, 220_000_000
        if items and any(pl_min <= int(getattr(p, "feed_order", 0) or 0) < pl_max for p in items):
            # 已由 _collect_profile_feed_anchor_rows 写入主页滚动/HTML 流顺序；尾屏 HTML 常被虚拟化，勿覆盖。
            return items
        pos = self._profile_post_url_first_positions(html)
        if not pos:
            return items
        ranked = sorted(items, key=lambda p: (pos.get(p.url, 10**15), p.feed_order))
        return [replace(p, feed_order=i) for i, p in enumerate(ranked)]

    def _merge_dom_with_html_probe(self, dom: list[PostItem], probe: list[PostItem]) -> list[PostItem]:
        """DOM + HTML 探针合并；顺序先按 DOM 出现，再补 probe 独有 URL。"""
        if not probe:
            return dom
        merged: dict[str, PostItem] = {}
        for p in dom:
            u = p.url
            merged[u] = p if u not in merged else self._prefer_post_item_merge(merged[u], p)
        for p in probe:
            u = p.url
            merged[u] = p if u not in merged else self._prefer_post_item_merge(merged[u], p)
        order: list[str] = []
        seen: set[str] = set()
        for p in dom:
            u = p.url
            if u not in seen and u in merged:
                order.append(u)
                seen.add(u)
        for p in probe:
            u = p.url
            if u not in seen and u in merged:
                order.append(u)
                seen.add(u)
        for u in merged:
            if u not in seen:
                order.append(u)
                seen.add(u)
        return [merged[u] for u in order]

    def _items_from_profile_feed_evaluate(self, anchors: list) -> list[PostItem]:
        items: list[PostItem] = []
        seen_urls: set[str] = set()
        for anchor in anchors or []:
            href = (anchor.get("href") or "").strip()
            text = (anchor.get("text") or "").strip()
            posted_at = (anchor.get("postedAt") or anchor.get("time") or "").strip()
            raw_ut = anchor.get("postedUt")
            posted_utime = 0
            try:
                if raw_ut is not None and raw_ut != "":
                    posted_utime = int(raw_ut)
            except (TypeError, ValueError):
                posted_utime = 0
            if posted_utime < 0:
                posted_utime = 0
            feed_order = 999999
            try:
                if anchor.get("order") is not None and anchor.get("order") != "":
                    feed_order = int(anchor.get("order"))
            except (TypeError, ValueError):
                feed_order = 999999
            try:
                ft = anchor.get("feedTop")
                if ft is not None:
                    ft_i = int(float(ft))
                    alt_top = 1_000_000 + min(max(0, ft_i), 8_999_999)
                    if feed_order >= 999_000_000 or feed_order == 999999:
                        feed_order = alt_top
                    else:
                        feed_order = min(feed_order, alt_top)
            except (TypeError, ValueError):
                pass
            if not href:
                continue
            normalized = self._normalize_url(href)
            if not self._looks_like_post_url(href, marketplace=False):
                continue
            if not self._is_in_target_scope(normalized):
                continue
            if normalized in seen_urls:
                continue
            if not self._passes_profile_allowlist(normalized):
                continue
            if not self._is_canonical_timeline_post_url(normalized):
                continue
            seen_urls.add(normalized)
            items.append(
                self._to_post_item(
                    normalized,
                    (text or "").strip(),
                    posted_at=posted_at,
                    posted_utime=posted_utime,
                    feed_order=feed_order,
                )
            )
        return items

    @staticmethod
    def _average_hash_png_bytes(png: bytes) -> str:
        try:
            from PIL import Image
        except Exception:
            return ""
        try:
            try:
                _resample = Image.Resampling.LANCZOS
            except AttributeError:
                _resample = Image.LANCZOS
            img = Image.open(io.BytesIO(png)).convert("L").resize((8, 8), _resample)
            px = list(img.getdata())
            avg = sum(px) / max(len(px), 1)
            bits = 0
            for i, p in enumerate(px):
                if p >= avg:
                    bits |= 1 << i
            return f"{bits:016x}"
        except Exception:
            return ""

    def _ocr_png_bytes(self, png: bytes) -> str:
        if not self._extra_feed_ocr_enabled or not png:
            return ""
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            return ""

        def _work() -> str:
            try:
                img = Image.open(io.BytesIO(png))
                return (
                    pytesseract.image_to_string(img, lang=self._extra_feed_ocr_lang) or ""
                ).strip()
            except Exception:
                return ""

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_work).result(timeout=75)
        except FuturesTimeoutError:
            return ""
        except Exception:
            return ""

    @staticmethod
    def _crop_posts_column(png: bytes, posts_col_left_css: int, viewport_width: int) -> bytes:
        """裁掉左栏（Details / 营业状态 / Contact info / Photos 等），只保留 Posts 这一列做 OCR。

        - 若 ``posts_col_left_css`` 来自精确 snap（>10），按该坐标裁；
        - 若 snap 没拿到精确值（=0/<=10），**fallback 到 viewport_width × 0.40**：
          FB 桌面布局下 Posts 列起点稳定在 35%~45%，固定 40% 既能砍掉左栏整列
          (Details / Open now / Edmonton / Photos…)，又不会切到右栏 Posts 内容。
          这是「OCR 把 Open now 误当成帖子正文」的根本修复点。
        """
        if not png:
            return png
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png))
            w, h = img.size
            dpr = max(1, round(w / viewport_width)) if viewport_width > 0 else 2
            if posts_col_left_css > 10:
                left_px = max(0, int(posts_col_left_css * dpr) - 16)
            else:
                # 兜底：从视口宽度 40% 处开始裁
                # 安全余量 -8px，避免把 Posts 标题/头像切一半
                fallback_css = max(200, int(viewport_width * 0.40))
                left_px = max(0, int(fallback_css * dpr) - 8)
            if left_px >= w - 100:
                return png
            cropped = img.crop((left_px, 0, w, h))
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return png

    def _ocr_png_bytes_direct(self, png: bytes, lang: str | None = None) -> str:
        """不依赖 EXTRA_FEED_OCR：对截图跑 Tesseract（需本机已安装 tesseract）。"""
        if not png:
            return ""
        use_lang = (lang or self._extra_feed_ocr_lang or "eng").strip() or "eng"
        try:
            import pytesseract
            from PIL import Image
        except Exception:
            return ""

        def _work() -> str:
            try:
                try:
                    _resample = Image.Resampling.LANCZOS
                except AttributeError:
                    _resample = Image.LANCZOS
                img = Image.open(io.BytesIO(png)).convert("RGB")
                w, h = img.size
                if w > 0 and w < 960:
                    img = img.resize((min(w * 2, 2000), min(h * 2, 2400)), _resample)
                return (pytesseract.image_to_string(img, lang=use_lang) or "").strip()
            except Exception:
                return ""

        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(_work).result(timeout=75)
        except FuturesTimeoutError:
            return ""
        except Exception:
            return ""

    def _profile_home_first_story_evaluate(self) -> dict:
        slug = self._target_profile_slug()
        if not slug or not self._page:
            return {}
        raw = self._page.evaluate(self._JS_FIRST_VISIBLE_STORY_SNAPSHOT, slug)
        return raw if isinstance(raw, dict) else {}

    def _load_profile_home_first_story_snapshot(self) -> dict:
        """打开主页 → 只把 **Posts 标题行**（含 Filters）滚进视口，**不在 feed 里向下滚**；取该行下方第一条帖。

        关键：登录返回后 FB 经常先送一个未渲染完的主页（feed 容器存在但 /posts/ 链接还没注水），
        因此首次拿不到 postUrl 时**强制 reload** 再试，最多两轮，避免每次都"登录成功→OCR 抓不到"。
        """
        slug = self._target_profile_slug()
        if not slug or not self._page:
            return {}
        # /posts tab: feed is the primary content, articles hydrate sooner than on root profile.
        posts_tab = self._posts_tab_url()
        load_url = posts_tab if posts_tab else f"https://www.facebook.com/{slug}"

        # Phase 1 – navigation + login.  First load is only for establishing the right URL and
        # handling any login wall; we do NOT evaluate here because BFCache / partial SSR content
        # makes the first evaluate unreliable.
        current_url = (self._page.url or "").split("?")[0].rstrip("/").lower()
        at_load_url = current_url == load_url.rstrip("/").lower()
        if at_load_url:
            self._page.reload(wait_until="domcontentloaded", timeout=20000)
        else:
            self._page.goto(load_url, wait_until="domcontentloaded", timeout=20000)
        self._page.wait_for_timeout(2500)
        did_login = self._attempt_auto_login()
        if did_login and posts_tab:
            try:
                self._page.goto(posts_tab, wait_until="domcontentloaded", timeout=30000)
                self._page.wait_for_timeout(2500)
                self._dismiss_post_login_interstitials()
            except Exception:
                pass
        else:
            self._dismiss_post_login_interstitials()

        # Phase 2 – true SPA load.  A second reload forces a fresh network load (BFCache consumed
        # by phase 1), so the SPA hydrates completely before we evaluate.  This is identical to
        # what the former "story_snapshot_retry" path always did, minus the error log.
        try:
            self._page.reload(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            try:
                self._page.goto(load_url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
        self._page.wait_for_timeout(2500)
        self._dismiss_post_login_interstitials()

        def _wait_for_feed_signals(timeout_posts_ms: int, timeout_feed_ms: int) -> None:
            try:
                self._page.wait_for_selector(
                    f'a[href*="/{slug}/posts/"]',
                    timeout=timeout_posts_ms,
                    state="attached",
                )
            except Exception:
                pass
            try:
                self._page.wait_for_selector(
                    '[role="main"] [role="feed"]',
                    timeout=timeout_feed_ms,
                    state="visible",
                )
            except Exception:
                pass

        _wait_for_feed_signals(10000, 6000)
        self._page.wait_for_timeout(2000)
        try:
            self._page.evaluate(self._JS_FOCUS_POSTS_SECTION_HEADER)
        except Exception:
            pass
        self._page.wait_for_timeout(900)
        snap = self._profile_home_first_story_evaluate()
        if isinstance(snap, dict) and snap.get("postUrl") and len((snap.get("domText") or "").strip()) >= 12:
            return snap
        # Fallback: wait a bit longer and try once more (covers rare slow hydration)
        self._page.wait_for_timeout(4000)
        snap2 = self._profile_home_first_story_evaluate()
        return snap2 if isinstance(snap2, dict) else snap

    @staticmethod
    def _extract_post_body_from_ocr(ocr: str) -> str:
        """从 OCR 杂讯里挑最像帖子正文的一行（与 Mezz…See more 类文案兼容）。"""
        raw = (ocr or "").replace("\r\n", "\n")
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.split("\n") if ln.strip()]
        noise = re.compile(
            r"^(photos|contact|message|following|search|filters?|hooked\s+(?:hooked\s+)?billiards|"
            r"closing soon|reviews?|edmonton|canada|followers?)$",
            re.I,
        )
        for ln in lines:
            if len(ln) < 16:
                continue
            if noise.match(ln[:48].strip()):
                continue
            if re.search(r"see more|查看更多|顯示更多|sold|cue|shaft|pool|sale|出售", ln, re.I):
                return ln[:650]
        best = ""
        for ln in lines:
            if len(ln) < 22 or noise.match(ln[:48].strip()):
                continue
            alnum = len(re.sub(r"[^\w\u4e00-\u9fff]", "", ln))
            if alnum > len(re.sub(r"[^\w\u4e00-\u9fff]", "", best)):
                best = ln
        return (best or "")[:650]

    def _find_url_from_items_matching_story_text(
        self, items: list[PostItem], dom: str, ocr_full: str
    ) -> str:
        """视口正文与爬虫列表对齐：仅当某条 crawl 正文出现在 DOM/OCR  blob 中才采用其 URL（避免 items[0] 错配）。"""
        ocr_body = self._extract_post_body_from_ocr(ocr_full)
        blob = re.sub(
            r"\s+",
            " ",
            " ".join([dom or "", ocr_full or "", ocr_body or ""]).lower(),
        ).strip()
        if len(blob) < 22:
            return ""
        for p in items:
            nu = self._normalize_url(p.url)
            if not self._is_canonical_timeline_post_url(nu):
                continue
            tx = re.sub(r"\s+", " ", (p.text or "").lower()).strip()
            if len(tx) < 16:
                continue
            for n in (90, 160, min(380, len(tx))):
                needle = tx[:n].strip()
                if len(needle) < 14:
                    continue
                if needle in blob:
                    return nu
        return ""

    def _prefer_viewport_first_story_items(self, items: list[PostItem]) -> list[PostItem]:
        """方案一：视口 Posts 下首条 **截图 OCR** 为准；其次 DOM；再其次同 URL 的爬虫正文。

        URL：快照 permalink 优先；否则仅当爬虫某条正文与 DOM/OCR 一致时才采用（绝不盲用 items[0]）。

        **不变量**：只要 ``self._page`` 还在，本方法必定写入一张截图到
        ``logs/fb_latest_post_story.png`` 并写入 OCR 文本（可能为空字符串）。
        这样上层永远拿到「这一轮的真实状态」，不会沿用 5 小时前的旧 OCR 把
        新帖当成「和上次一样」漏掉。
        """
        self._reset_top_story_state()
        if self.is_marketplace_target or not items or not self._page:
            return items

        snap: dict = {}
        try:
            snap = self._load_profile_home_first_story_snapshot() or {}
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] story_snapshot_failed: {exc!s} (将继续走视口兜底截图)",
                flush=True,
            )

        dom = (snap.get("domText") or "").strip() if isinstance(snap, dict) else ""
        url_raw = (snap.get("postUrl") or "").strip() if isinstance(snap, dict) else ""
        nu = self._normalize_url(url_raw) if url_raw else ""
        clip = snap.get("clip") if isinstance(snap, dict) else None
        posts_col_left = int(snap.get("postsColumnLeft") or 0) if isinstance(snap, dict) else 0
        vp = self._page.viewport_size or {}
        vw = int(vp.get("width") or 1280)
        vh = int(vp.get("height") or 800)

        story_png: bytes | None = None
        ocr_txt = ""

        # ① 优先用 snap 指定的 clip 截首条
        try:
            if (
                isinstance(clip, dict)
                and int(clip.get("width") or 0) >= 60
                and int(clip.get("height") or 0) >= 32
            ):
                x = max(0, int(clip["x"]))
                y = max(0, int(clip["y"]))
                w = min(int(clip["width"]), vw - x)
                h = min(int(clip["height"]), vh - y)
                if w >= 60 and h >= 32:
                    png = self._page.screenshot(
                        type="png",
                        clip={"x": x, "y": y, "width": w, "height": h},
                    )
                    story_png = png
                    ocr_png = self._crop_posts_column(png, posts_col_left, vw)
                    ocr_txt = self._ocr_png_bytes_direct(ocr_png) or ""
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] story_clip_screenshot_failed: {exc!s} (继续走视口兜底)",
                flush=True,
            )

        # ② 不论 snap 是否拿到 clip：只要 OCR 还为空，就拍一张视口上半区作为兜底。
        #    从 y=60 开始跳过 FB 固定导航栏（~56px），避免「2 8 &」等导航噪声污染 OCR。
        if not story_png or not ocr_txt:
            try:
                _fb_nav_h = 60  # FB 固定导航栏高度约 56px，多 4px 保险
                png2 = self._page.screenshot(
                    type="png",
                    clip={
                        "x": 0,
                        "y": _fb_nav_h,
                        "width": max(200, vw),
                        "height": min(int(vh * 0.94) - _fb_nav_h, max(400, vh - _fb_nav_h - 40)),
                    },
                )
                if not story_png:
                    story_png = png2
                if not ocr_txt:
                    ocr_png2 = self._crop_posts_column(png2, posts_col_left, vw)
                    ocr_txt = self._ocr_png_bytes_direct(ocr_png2) or ""
            except Exception as exc:
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{ts}] story_viewport_screenshot_failed: {exc!s} (本轮 OCR 视为空)",
                    flush=True,
                )

        # ③ 截图必落盘 + 哈希，方便人工对照、上层 hamming 比对、以及 mtime 巡检
        if story_png:
            try:
                self.last_top_post_story_clip_hash = self._average_hash_png_bytes(story_png) or ""
            except Exception:
                self.last_top_post_story_clip_hash = ""
            try:
                self._persist_latest_fb_story_png(story_png)
            except OSError as exc:
                ts = datetime.now().strftime("%H:%M:%S")
                print(
                    f"[{ts}] story_screenshot_persist_failed: {exc!s} (将无法落盘最新截图)",
                    flush=True,
                )
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] story_screenshot_empty: 视口截图也未拿到 (page 可能崩溃)",
                flush=True,
            )

        self.last_top_post_story_dom_text = dom
        self.last_top_post_story_ocr_text = ocr_txt

        url_final = nu
        if not url_final and items:
            url_final = self._find_url_from_items_matching_story_text(items, dom, ocr_txt)
        if not url_final:
            self.last_top_post_story_post_url = ""
            return items

        self.last_top_post_story_post_url = url_final

        crawl_txt = ""
        for p in items:
            if self._normalize_url(p.url) == url_final:
                crawl_txt = (p.text or "").strip()
                break

        ocr_body = self._extract_post_body_from_ocr(ocr_txt)
        if len(ocr_body) >= 12:
            body = ocr_body[:700]
        elif len(dom) >= 12:
            body = dom[:700]
        elif len(crawl_txt) >= 12:
            body = crawl_txt[:700]
        else:
            body = (ocr_body or dom or crawl_txt or "")[:700]

        live = self._to_post_item(url_final, body, feed_order=500_000)
        rest = [p for p in items if self._normalize_url(p.url) != url_final]
        return [live] + rest

    # OCR 文本里出现这些短语 → 视为登录墙/错误页/截图无内容，不算「成功抓到首条」
    _LOGIN_OR_ERROR_OCR_HINTS: tuple[str, ...] = (
        "log in to facebook",
        "log into facebook",
        "create new account",
        "forgotten password",
        "forgot password",
        "you must log in",
        "see more on facebook",
        "we are working on getting this fixed",
        "this content isn't available",
        "this page isn't available",
        "page not found",
        "we suspect automated behavior",
        "session expired",
        "请登录",
        "登入 facebook",
        "登录 facebook",
        "此页面无法使用",
    )

    @classmethod
    def _looks_like_login_or_error_screen(cls, ocr_text: str) -> bool:
        low = (ocr_text or "").lower()
        if not low:
            return False
        return any(hint in low for hint in cls._LOGIN_OR_ERROR_OCR_HINTS)

    def _capture_top_story_png_and_ocr(
        self,
        clip: dict | None,
        posts_col_left: int,
        vw: int,
        vh: int,
    ) -> tuple[bytes | None, str]:
        """先按 snap.clip 截首条；不行则全视口上半区。返回 (png 字节, OCR 文本)。"""
        story_png: bytes | None = None
        ocr_txt = ""
        if not self._page:
            return None, ""
        try:
            if (
                isinstance(clip, dict)
                and int(clip.get("width") or 0) >= 60
                and int(clip.get("height") or 0) >= 32
            ):
                x = max(0, int(clip["x"]))
                y = max(0, int(clip["y"]))
                w = min(int(clip["width"]), vw - x)
                h = min(int(clip["height"]), vh - y)
                if w >= 60 and h >= 32:
                    png = self._page.screenshot(
                        type="png",
                        clip={"x": x, "y": y, "width": w, "height": h},
                        timeout=20000,
                    )
                    story_png = png
                    ocr_png = self._crop_posts_column(png, posts_col_left, vw)
                    ocr_txt = self._ocr_png_bytes_direct(ocr_png) or ""
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] story_clip_screenshot_failed: {exc!s}", flush=True)

        if not story_png or not ocr_txt:
            try:
                _nav_h = 56  # 跳过 FB 固定导航栏（~56px），避免「2 8 &」导航噪声污染 OCR
                # 最多截 300px（帖子标题+正文区），不延伸到图片/评论区，避免图片 OCR 污染
                _max_text_h = 300
                png2 = self._page.screenshot(
                    type="png",
                    clip={
                        "x": 0,
                        "y": _nav_h,
                        "width": max(200, vw),
                        "height": min(_max_text_h, vh - _nav_h - 10),
                    },
                    timeout=20000,
                )
                if not story_png:
                    story_png = png2
                if not ocr_txt:
                    ocr_png2 = self._crop_posts_column(png2, posts_col_left, vw)
                    ocr_txt = self._ocr_png_bytes_direct(ocr_png2) or ""
            except Exception as exc:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] story_viewport_screenshot_failed: {exc!s}", flush=True)

        return story_png, ocr_txt

    def _fetch_top_story_via_screenshot_only(self, max_attempts: int = 3) -> list[PostItem]:
        """**Profile 页（非 marketplace）的唯一抓取路径**：只用截图 + OCR，不再做 HTML/anchor 抽取。

        每轮 attempt：
          1. ``_load_profile_home_first_story_snapshot`` 触发 goto + 自动登录 + 弹窗清理 + reload retry
          2. 优先按 snap.clip 截首条；不行就视口上半区
          3. 截图必落盘到 ``logs/fb_latest_post_story.png``
          4. OCR；判定成功 = OCR ≥ 12 字符且不是登录/错误页文案

        失败 → 关闭浏览器会话（下一次 attempt 会重建 + 完整 auto-login）。
        全部 attempts 失败 → 置 ``last_screenshot_persistent_failure = True``，
        让 ``main.py`` 走「需要人工介入」告警；同步 ``last_login_wall_hint = True``
        以复用现有 session_alert 通道。
        """
        self.last_login_wall_hint = False
        self.last_screenshot_persistent_failure = False
        self._reset_top_story_state()
        if self.is_marketplace_target:
            return []
        self._ensure_browser()
        if not self._page:
            self.last_screenshot_persistent_failure = True
            return []

        last_dom = ""
        last_url = ""
        for attempt in range(1, max_attempts + 1):
            ts = datetime.now().strftime("%H:%M:%S")
            snap: dict = {}
            try:
                snap = self._load_profile_home_first_story_snapshot() or {}
            except Exception as exc:
                print(
                    f"[{ts}] story_only attempt {attempt}/{max_attempts} snapshot exception: {exc!s}",
                    flush=True,
                )

            dom = (snap.get("domText") or "").strip() if isinstance(snap, dict) else ""
            url_raw = (snap.get("postUrl") or "").strip() if isinstance(snap, dict) else ""
            nu = self._normalize_url(url_raw) if url_raw else ""
            clip = snap.get("clip") if isinstance(snap, dict) else None
            posts_col_left = int(snap.get("postsColumnLeft") or 0) if isinstance(snap, dict) else 0
            vp = self._page.viewport_size or {}
            vw = int(vp.get("width") or 1280)
            vh = int(vp.get("height") or 800)

            story_png, ocr_txt = self._capture_top_story_png_and_ocr(
                clip, posts_col_left, vw, vh
            )

            # 不变量：每轮无论成败，截图必落盘（方便人工诊断）
            if story_png:
                try:
                    self.last_top_post_story_clip_hash = (
                        self._average_hash_png_bytes(story_png) or ""
                    )
                except Exception:
                    self.last_top_post_story_clip_hash = ""
                try:
                    self._persist_latest_fb_story_png(story_png)
                except OSError as exc:
                    print(
                        f"[{ts}] story_screenshot_persist_failed: {exc!s}",
                        flush=True,
                    )

            self.last_top_post_story_dom_text = dom
            self.last_top_post_story_ocr_text = ocr_txt or ""
            last_dom = dom
            last_url = nu

            ocr_clean = (ocr_txt or "").strip()
            is_error_screen = self._looks_like_login_or_error_screen(ocr_clean)
            success = len(ocr_clean) >= 12 and not is_error_screen

            if success:
                if attempt > 1:
                    print(
                        f"[{ts}] story_only: success on attempt {attempt}/{max_attempts} "
                        f"(ocr_len={len(ocr_clean)})",
                        flush=True,
                    )
                ocr_body = (
                    self._extract_post_body_from_ocr(ocr_txt)
                    if ocr_txt
                    else ""
                )
                body = (
                    ocr_body
                    if len(ocr_body) >= 12
                    else (dom if len(dom) >= 12 else ocr_clean[:700])
                )
                url_final = nu or self.target_url
                self.last_top_post_story_post_url = url_final
                item = self._to_post_item(url_final, body[:700], feed_order=500_000)
                return [item]

            reason = (
                "OCR 像登录/错误页（命中已知短语）"
                if is_error_screen
                else f"OCR 文本过短（{len(ocr_clean)} 字）"
            )
            print(
                f"[{ts}] story_only attempt {attempt}/{max_attempts} 失败: {reason}; "
                f"url={nu or '(none)'} dom_len={len(dom)}",
                flush=True,
            )

            if attempt < max_attempts:
                # 强制重建浏览器会话：close 后下一次 _ensure_browser 触发完整登录
                print(
                    f"[{ts}] story_only: 关闭浏览器会话准备重登 (attempt {attempt + 1}/{max_attempts} 即将开始)",
                    flush=True,
                )
                try:
                    self.close()
                except Exception:
                    pass
                self._ensure_browser()
                if not self._page:
                    break
                try:
                    self._page.wait_for_timeout(5000)
                except Exception:
                    pass

        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"[{ts}] story_only: ALL {max_attempts} attempts FAILED → 设置持续告警标志 "
            f"(最后一次 url={last_url or '(none)'} dom_len={len(last_dom)})",
            flush=True,
        )
        self.last_screenshot_persistent_failure = True
        self.last_login_wall_hint = True
        return []

    def read_latest_post_story_via_browser_ocr(self, screenshot_path: str | None = None) -> str:
        """打开主页，首条 story：**截图 OCR 优先**，DOM 兜底；写入 ``last_top_post_story_*``。

        ``screenshot_path`` 若给出则写入 PNG（便于单次人工对照，非长期配置）。
        """
        self._reset_top_story_state()
        if not self.use_browser_mode or self.is_marketplace_target:
            return ""
        slug = self._target_profile_slug()
        if not slug:
            return ""
        self._ensure_browser()
        assert self._page is not None
        try:
            snap = self._load_profile_home_first_story_snapshot()
            dom = (snap.get("domText") or "").strip()
            url = (snap.get("postUrl") or "").strip()
            self.last_top_post_story_dom_text = dom
            self.last_top_post_story_post_url = self._normalize_url(url) if url else ""
            clip = snap.get("clip")
            posts_col_left = int(snap.get("postsColumnLeft") or 0)
            vp = self._page.viewport_size or {}
            vw = int(vp.get("width") or 1280)
            vh = int(vp.get("height") or 800)
            png: bytes | None = None
            if (
                isinstance(clip, dict)
                and clip.get("width", 0) >= 60
                and clip.get("height", 0) >= 32
            ):
                x = max(0, int(clip["x"]))
                y = max(0, int(clip["y"]))
                w = min(int(clip["width"]), vw - x)
                h = min(int(clip["height"]), vh - y)
                if w >= 60 and h >= 32:
                    png = self._page.screenshot(
                        type="png",
                        clip={"x": x, "y": y, "width": w, "height": h},
                    )
            if not png:
                png = self._page.screenshot(
                    type="png",
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": max(200, vw),
                        "height": min(int(vh * 0.94), max(400, vh - 40)),
                    },
                )
            if screenshot_path and png:
                self._persist_latest_fb_story_png(png, dest=Path(screenshot_path))
            ocr_png = self._crop_posts_column(png, posts_col_left, vw) if png else b""
            ocr = self._ocr_png_bytes_direct(ocr_png) if ocr_png else ""
            self.last_top_post_story_ocr_text = ocr
            ocr_pick = self._extract_post_body_from_ocr(ocr) if ocr else ""
            if len(ocr_pick) >= 12:
                return ocr_pick
            if len(dom) > 14:
                return dom
            return (ocr_pick or ocr or dom or "")
        except Exception as exc:
            ts = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{ts}] story_ocr_via_browser_failed: {exc!s} (清空缓存)",
                flush=True,
            )
            self._reset_top_story_state()
            return ""

    def _reorder_posts_by_ocr_overlap(self, posts: list[PostItem], ocr_raw: str) -> list[PostItem]:
        if len(posts) < 2 or not (ocr_raw or "").strip():
            return posts
        ocr = re.sub(r"\s+", " ", ocr_raw.lower())
        ocr = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", ocr)
        ocr = re.sub(r"\s+", " ", ocr).strip()
        if len(ocr) < 20:
            return posts
        o_tokens = {w for w in ocr.split() if len(w) > 2}

        def score(p: PostItem) -> int:
            t = re.sub(r"\s+", " ", (p.text or "").lower())
            if not t:
                return 0
            head = t[:120].strip()
            if len(head) > 12 and head in ocr[:600]:
                return 5000 + len(head)
            tw = {w for w in t.split() if len(w) > 2}
            return len(o_tokens & tw)

        return sorted(posts, key=lambda p: (-score(p), p.feed_order))

    def _capture_feed_top_visual_hash(self, clip_height: int = 900) -> None:
        """视口顶 PNG：平均哈希 + 可选 OCR（并行）；用于多手段对比。"""
        self._last_feed_top_visual_hash = ""
        self._last_feed_top_ocr_text = ""
        if not self._page or self.is_marketplace_target:
            return
        try:
            self._page.evaluate("window.scrollTo(0, 0)")
            self._page.wait_for_timeout(400)
            vp = self._page.viewport_size or {}
            vw = int(vp.get("width") or 1280)
            vh = min(int(clip_height), int(vp.get("height") or 800))
            png = self._page.screenshot(
                type="png",
                clip={"x": 0, "y": 0, "width": max(200, vw), "height": max(200, vh)},
            )
            ocr_on = self._extra_feed_ocr_enabled
            with ThreadPoolExecutor(max_workers=2 if ocr_on else 1) as ex:
                fut_h = ex.submit(self._average_hash_png_bytes, png)
                fut_o = ex.submit(self._ocr_png_bytes, png) if ocr_on else None
                self._last_feed_top_visual_hash = fut_h.result() or ""
                self._last_feed_top_ocr_text = (fut_o.result() if fut_o else "") or ""
        except Exception:
            self._last_feed_top_visual_hash = ""
            self._last_feed_top_ocr_text = ""

    def _sort_posts_by_recency_evidence(self, posts: list[PostItem]) -> list[PostItem]:
        """多帖时：可信 utime > 主页滚动/HTML 流顺序(Comet 时间常错) > 相对时间 > 其它。"""
        if len(posts) < 2:
            return posts
        now = int(time.time())
        recent_cut = now - 7 * 86400
        pl_min, pl_max = 1_000_000, 220_000_000

        def sk(p: PostItem) -> tuple:
            u = p.posted_utime or 0
            if u >= recent_cut:
                return (0, -u, 0, p.feed_order)
            fe = int(p.feed_order or 0)
            if pl_min <= fe < pl_max:
                # 由 _collect_profile_feed_anchor_rows 写入：更小 ≈ 更靠近 Posts 顶部 ≈ 更新
                return (1, fe, 0, 0)
            la = _fb_label_age_seconds(p.posted_at)
            if la is not None:
                return (2, la, 0, p.feed_order)
            return (3, 999999999, p.feed_order, -u)

        return sorted(posts, key=sk)

    def _return_profile_posts_with_visual(self, items: list[PostItem]) -> list[PostItem]:
        items = self._sort_posts_by_recency_evidence(items)
        self._capture_feed_top_visual_hash(clip_height=self._feed_visual_clip_height)
        if (
            self._extra_feed_ocr_enabled
            and len(items) > 1
            and (self._last_feed_top_ocr_text or "").strip()
        ):
            items = self._reorder_posts_by_ocr_overlap(items, self._last_feed_top_ocr_text)
            items = self._sort_posts_by_recency_evidence(items)
        # Extra 等「不滚动」模式：对外只保留排序后的首条
        if int(getattr(self, "_profile_feed_scroll_passes", 11)) == 0 and items:
            items = items[:1]
        return items

    def _collect_profile_feed_anchor_rows(
        self, js_profile_feed: str, profile_slug: str
    ) -> tuple[list[dict], list[PostItem]]:
        """Scroll the profile feed and merge repeated JS/HTML snapshots.

        Comet virtualizes stories: scrolling back to the top often leaves only one
        ``a[href*=/posts/]`` in the DOM. Sampling during the scroll pass unions URLs
        and keeps the richest text/time row per href.
        """
        merged: dict[str, dict] = {}
        probe_union: list[PostItem] = []
        probe_urls: set[str] = set()
        # Comet time labels are often wrong on the wrong story shell; earliest scroll step +
        # HTML stream offset matches "Posts" order (newest higher on page → smaller step/pos).
        first_step_by_url: dict[str, int] = {}
        stream_pos_by_url: dict[str, int] = {}

        def _norm_post_url(href: str) -> str:
            return self._normalize_url((href or "").split("?", 1)[0].strip())

        def note_url_placement(href: str, step_idx: int, html: str) -> None:
            u = _norm_post_url(href)
            if not u or "/posts/" not in u:
                return
            prev_s = first_step_by_url.get(u)
            fp = self._profile_post_url_first_positions(html) if html else {}
            pos = int(fp.get(u, 10**9))
            if prev_s is None or step_idx < prev_s:
                first_step_by_url[u] = step_idx
                stream_pos_by_url[u] = pos
            elif step_idx == prev_s:
                stream_pos_by_url[u] = min(stream_pos_by_url.get(u, 10**9), pos)

        def row_score(row: dict) -> tuple:
            try:
                ut = int(row.get("postedUt") or 0)
            except (TypeError, ValueError):
                ut = 0
            pa = (row.get("postedAt") or row.get("time") or "").strip()
            la = _fb_label_age_seconds(pa)
            ag = la if la is not None else 10**9
            tx = (row.get("text") or "").strip()
            sub = FacebookPostMonitor._feed_body_substance_score(tx)
            tl = len(tx)
            # substance before raw length so "1 reaction…" never beats a real caption
            return (ut, -ag, sub, tl)

        def absorb_row(row: dict | None) -> None:
            if not row:
                return
            href = (row.get("href") or "").strip()
            if not href:
                return
            key = _norm_post_url(href)
            if not key or "/posts/" not in key:
                return
            row = dict(row)
            row["href"] = key
            prev = merged.get(key)
            if prev is None:
                merged[key] = row
                return
            chosen = dict(row) if row_score(row) > row_score(prev) else dict(prev)
            tmin = None
            for src in (row, prev):
                try:
                    v = src.get("feedTop")
                    if v is None:
                        continue
                    fv = float(v)
                    tmin = fv if tmin is None else min(tmin, fv)
                except (TypeError, ValueError):
                    pass
            if tmin is not None:
                chosen["feedTop"] = int(tmin)
            merged[key] = chosen

        def absorb_probe_html(html: str) -> None:
            if not html:
                return
            for p in self._serialized_html_post_probe(html):
                if p.url in probe_urls:
                    continue
                probe_urls.add(p.url)
                probe_union.append(p)

        def note_all_posts_in_html(html: str, step_idx: int) -> None:
            if not html:
                return
            for u in self._profile_post_url_first_positions(html):
                note_url_placement(u, step_idx, html)

        if not self._page:
            return [], []

        self._page.evaluate("window.scrollTo(0, 0)")
        self._page.wait_for_timeout(3200)
        html0 = self._page.content()
        try:
            batch0 = self._page.evaluate(js_profile_feed, profile_slug) or []
        except Exception:
            batch0 = []
        step0 = 0
        for r in batch0:
            absorb_row(r)
            note_url_placement(r.get("href", ""), step0, html0)
        absorb_probe_html(html0)
        note_all_posts_in_html(html0, step0)

        n_extra = int(getattr(self, "_profile_feed_scroll_passes", 11))
        n_extra = max(0, min(20, n_extra))
        for step_i in range(1, n_extra + 1):
            self._page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9));")
            self._page.wait_for_timeout(850)
            html = self._page.content()
            try:
                batch = self._page.evaluate(js_profile_feed, profile_slug) or []
            except Exception:
                batch = []
            for r in batch:
                absorb_row(r)
                note_url_placement(r.get("href", ""), step_i, html)
            absorb_probe_html(html)
            note_all_posts_in_html(html, step_i)

        # feed_order ≥ 1_000_000 encodes scroll placement (smaller = nearer top of profile ≈ newer).
        out_rows: list[dict] = []
        for row in merged.values():
            href = (row.get("href") or "").strip()
            u = _norm_post_url(href)
            st = int(first_step_by_url.get(u, 99))
            sp = int(stream_pos_by_url.get(u, 10**9))
            if st < 90:
                spr = sp if sp < 10**9 else (abs(hash(u)) % 8_999_001)
                enc = 1_000_000 + st * 10_000_000 + min(int(spr), 9_999_999)
            else:
                enc = 999_999_999
            row2 = dict(row)
            row2["firstStep"] = st
            row2["streamPos"] = sp
            row2["order"] = enc
            out_rows.append(row2)

        return out_rows, probe_union

    def _fetch_posts_browser(self) -> list[PostItem]:
        self.last_login_wall_hint = False
        self._last_feed_top_visual_hash = ""
        self._last_feed_top_ocr_text = ""
        self._ensure_browser()

        # ---- Profile 模式：**唯一**抓取路径 = 截图 + OCR ----
        # FB 改版导致 /posts/ anchor、Comet props 都极不稳定，HTML 抽取经常拿到旧数据或空列表，
        # 已彻底删除其作为「主路径」。所有 profile 监控都强制走截图，截图失败会自动重登并最多重试 3 次，
        # 全部失败 → 置 last_screenshot_persistent_failure，让 main.py 发「需要人工介入」告警。
        if not self.is_marketplace_target:
            items = self._fetch_top_story_via_screenshot_only(max_attempts=3)
            if self._page:
                try:
                    self._capture_feed_top_visual_hash(
                        clip_height=self._feed_visual_clip_height
                    )
                except Exception:
                    pass
            return items

        # ---- Marketplace 模式：保留原 HTML 抽取（页面结构相对稳定） ----
        self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=20000)
        self._page.wait_for_timeout(1500)
        if self._attempt_auto_login():
            self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=20000)
        self._page.wait_for_timeout(1500)

        if False:  # legacy_profile_html_path_disabled: kept as dead code for diff context (will be removed in a follow-up)
            # Collect from the main feed: canonical /posts/ URL in Python; best-effort body + time in JS.
            js_profile_feed = """
            (profileSlug) => {
              const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
              /** 只认主帖路径，去掉 query，避免 comment_id / 追踪参数 造成重复与误排序 */
              const canonicalPostUrl = (href) => {
                try {
                  const u = new URL(href || "", location.href);
                  const pn = (u.pathname || "").replace(/\\/+$/, "");
                  if (!/\\/posts\\//i.test(pn)) return "";
                  return "https://www.facebook.com" + pn;
                } catch (e) {
                  return "";
                }
              };
              const isCommentOrTrackingHref = (href) => {
                try {
                  const q = (new URL(href || "", location.href).search || "").toLowerCase();
                  return /comment_id|reply_comment_id|comment_tracking|ft_ent/.test(q);
                } catch (e) {
                  return false;
                }
              };
              const reactionOnlyBody = (text) => {
                const s = clean(text || "").toLowerCase();
                if (s.length < 8) return false;
                if (/^see who reacted/.test(s)) return true;
                if (/^\\d+\\s+reactions?\\b/.test(s) && s.includes("see who reacted")) return true;
                if (/^\\d+\\s+reactions?\\b/.test(s) && s.length < 90) return true;
                return false;
              };
              const isNoiseTimeText = (t) => {
                const x = clean(t || "").toLowerCase();
                if (!x) return false;
                return /reaction|see who reacted|view\\s+\\d+\\s+comments|条评论|所有心情|commented on/.test(x);
              };
              const isPostHref = (href) => {
                if (!href) return false;
                return /\\/posts\\//i.test(href);
              };
              const depthWithin = (root, node) => {
                let d = 0;
                let n = node;
                while (n && n !== root) {
                  d++;
                  n = n.parentElement;
                }
                return n === root ? d : 999;
              };
              /** Strip nested comment threads so time/body are from the page post only, not replies. */
              const storyShell = (el) => {
                try {
                  const c = el.cloneNode(true);
                  c.querySelectorAll("div[role='article']").forEach((sub) => {
                    if (sub !== c) sub.remove();
                  });
                  try {
                    c.querySelectorAll("div[role='toolbar']").forEach((sub) => sub.remove());
                    c.querySelectorAll("a[href*='comment_id']").forEach((sub) => sub.remove());
                  } catch (e2) {}
                  return c;
                } catch (e) {
                  return el;
                }
              };
              const utimeToLabel = (secStr) => {
                const sec = parseInt(secStr, 10);
                if (!sec || isNaN(sec)) return "";
                try {
                  return new Date(sec * 1000).toLocaleString(undefined, {
                    year: "numeric",
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  });
                } catch (e) {
                  return "";
                }
              };
              const timeFromPostAnchor = (a) => {
                if (!a) return "";
                const lab = (a.getAttribute("aria-label") || a.getAttribute("title") || "").trim();
                if (lab.length > 3 && lab.length < 220) {
                  if (isNoiseTimeText(lab)) return "";
                  return lab.slice(0, 180);
                }
                const ut = a.getAttribute("data-utime");
                if (ut && /^\\d+$/.test(ut)) {
                  const u = utimeToLabel(ut);
                  if (u) return u;
                }
                const full = clean(a.innerText || a.textContent || "");
                if (
                  full.length > 10 &&
                  full.length < 220 &&
                  /\\d{4}|yesterday|今天|昨天|\\b(mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\\b|\\d{1,2}[:.]\\d{2}|am\\b|pm\\b|上午|下午/i.test(
                    full
                  )
                ) {
                  return full.slice(0, 180);
                }
                if (full.length >= 1 && full.length <= 12 && /^\\d+[mhdw]$/i.test(full)) return full;
                if (full.length >= 1 && full.length <= 14 && /^just now|^now$/i.test(full)) return full;
                const line = (full.split(/\\r?\\n/)[0] || "").trim();
                if (
                  line.length > 8 &&
                  line.length < 180 &&
                  !/like\\s*·\\s*reply/i.test(line) &&
                  !/^\\d+[mhdw]$/i.test(line)
                )
                  return line.slice(0, 180);
                return "";
              };
              const timeScore = (t) => {
                if (!t) return -1;
                const tr = (t || "").trim();
                /** FB 动态头常见「27m」「3h」— 必须优先于长 locale 日期或其它 data-utime 误匹配 */
                if (/^\\d{1,4}[mhdw]$/i.test(tr)) return 102;
                if (/\\d{4}-\\d{2}-\\d{2}/.test(t)) return 100;
                if (/yesterday|今天|昨天|\\b(mon|tue|wed|thu|fri|sat|sun)\\b.{0,30}\\d{1,2}/i.test(t))
                  return 92;
                if (t.length > 40) return 80;
                if (/\\d{1,2}[:.]\\d{2}/.test(t)) return 75;
                if (t.length > 15) return 55;
                return 35;
              };
              const timeFromStory = (el, postAnchor) => {
                const cands = [];
                const push = (t, depth) => {
                  const x = clean(t || "");
                  if (x.length < 2) return;
                  if (isNoiseTimeText(x)) return;
                  cands.push({ t: x.slice(0, 180), depth, sc: timeScore(x) });
                };
                push(timeFromPostAnchor(postAnchor), 0);
                for (const n of el.querySelectorAll("[data-utime]")) {
                  const u = n.getAttribute("data-utime");
                  if (u && /^\\d+$/.test(u)) push(utimeToLabel(u), depthWithin(el, n));
                }
                for (const tm of el.querySelectorAll("time[datetime], time[title]")) {
                  const tit = (tm.getAttribute("title") || "").trim();
                  const dt = (tm.getAttribute("datetime") || "").trim();
                  const inner = clean(tm.innerText || tm.textContent || "");
                  const d = depthWithin(el, tm);
                  if (tit.length > 3) push(tit, d);
                  else if (inner.length > 2 && inner.length < 120) push(inner, d);
                  else if (dt.length > 8) push(dt, d);
                }
                for (const abbr of el.querySelectorAll("abbr[data-tooltip-content], abbr[title]")) {
                  const t = (
                    abbr.getAttribute("data-tooltip-content") ||
                    abbr.getAttribute("title") ||
                    ""
                  ).trim();
                  if (t.length > 3) push(t, depthWithin(el, abbr));
                }
                for (const sp of el.querySelectorAll("span, a, abbr, div")) {
                  const t = clean(sp.innerText || sp.textContent || "").trim();
                  if (/^\\d{1,4}[mhdw]$/i.test(t)) push(t, depthWithin(el, sp));
                }
                if (!cands.length) return "";
                cands.sort((a, b) => {
                  if (b.sc !== a.sc) return b.sc - a.sc;
                  return a.depth - b.depth;
                });
                return cands[0].t;
              };
              const bodyFromStory = (el) => {
                const selectors = [
                  "[data-ad-preview='message']",
                  "[data-ad-comet-preview='message']",
                  "[data-ad-rendering-role='story_message']",
                  "div[data-ad-rendering-role='story_message']",
                  "[data-testid='post_message']",
                  "[data-ad-rendering-role='ui_attachment_story_message']",
                  "[data-ad-rendering-role='message']",
                ];
                let best = "";
                for (const sel of selectors) {
                  try {
                    el.querySelectorAll(sel).forEach((node) => {
                      const t = clean(node.innerText || node.textContent || "");
                      if (t.length > best.length && t.length < 1800) best = t;
                    });
                  } catch (e) {}
                }
                try {
                  el.querySelectorAll("[aria-label], [title]").forEach((node) => {
                    const al = clean(
                      (node.getAttribute && node.getAttribute("aria-label")) ||
                        (node.getAttribute && node.getAttribute("title")) ||
                        ""
                    );
                    if (
                      al.length > best.length &&
                      al.length > 28 &&
                      al.length < 5000 &&
                      !/^like\\b|^comment\\b|^share\\b|^more\\b/i.test(al)
                    )
                      best = al;
                  });
                } catch (e) {}
                const trimSeeMore = (t) =>
                  clean((t || "").replace(/\\s*…?\\s*See more\\s*$/i, "").replace(/\\s*See more\\s*$/i, ""));
                if (best.length > 2) return trimSeeMore(best).slice(0, 420);
                const junk = /^(like|share|comment|评论|赞|发送)$/i;
                const autos = Array.from(el.querySelectorAll("span[dir='auto'], div[dir='auto']"))
                  .map((n) => clean(n.innerText || n.textContent || "").trim())
                  .filter((t) => t.length > 8 && t.length < 600 && !junk.test(t));
                autos.sort((a, b) => b.length - a.length);
                if (autos.length) return trimSeeMore(autos[0]).slice(0, 420);
                try {
                  const clone = el.cloneNode(true);
                  clone.querySelectorAll("div[role='article']").forEach((sub) => {
                    if (sub !== clone) sub.remove();
                  });
                  const raw = clone.innerText || clone.textContent || "";
                  const lines = raw.split(/\\r?\\n/).map((x) => clean(x)).filter(Boolean);
                  const kept = [];
                  for (const line of lines) {
                    if (/\\d+[hdmw]\\s*[·.]?\\s*Like\\s*[·.]?\\s*Reply/i.test(line)) break;
                    kept.push(line);
                  }
                  let t = clean(kept.join(" "));
                  if (t.length > 12) return trimSeeMore(t).slice(0, 420);
                } catch (e) {}
                const rough = clean((el.textContent || el.innerText || "").slice(0, 900));
                if (rough.length > 8) return trimSeeMore(rough).slice(0, 420);
                return "";
              };
              const pickShallowPostAnchor = (el) => {
                let best = null;
                let bestD = 999;
                for (const a of el.querySelectorAll("a[href]")) {
                  const href = a.href || "";
                  if (!/\\/posts\\//i.test(href)) continue;
                  if (isCommentOrTrackingHref(href)) continue;
                  const d = depthWithin(el, a);
                  if (d < bestD) {
                    bestD = d;
                    best = a;
                  }
                }
                return best;
              };
              /**
               * data-utime in shell: use the newest plausible stamp (max), not min —
               * min often grabbed an ancient embed/widget and made every post look "too old" to push.
               */
              const postUtFromShell = (shell, postA) => {
                const nowSec = Math.floor(Date.now() / 1000);
                const vals = [];
                const add = (n) => {
                  if (!n || !n.getAttribute) return;
                  const v = n.getAttribute("data-utime");
                  if (v && /^\\d+$/.test(v)) {
                    const t = parseInt(v, 10);
                    if (t > 1e9 && t < nowSec + 86400) vals.push(t);
                  }
                };
                add(postA);
                const msgRoot =
                  shell.querySelector(
                    "[data-ad-preview='message'],[data-ad-comet-preview='message'],[data-ad-rendering-role='story_message']"
                  ) || shell;
                msgRoot.querySelectorAll("[data-utime]").forEach((n) => add(n));
                if (!vals.length) return 0;
                const oldestOk = nowSec - 86400 * 400;
                const reasonable = vals.filter((t) => t >= oldestOk && t <= nowSec + 120);
                const pool = reasonable.length ? reasonable : vals;
                const anchorV = postA && postA.getAttribute("data-utime");
                if (anchorV && /^\\d+$/.test(anchorV)) {
                  const at = parseInt(anchorV, 10);
                  if (pool.includes(at)) return at;
                }
                return Math.max.apply(null, pool);
              };

              /**
               * 主页里「Posts」标题正下方的时间线才是用户要的最新动态；不要用侧栏/Reels 里 article 更多的 feed。
               */
              const findFeedUnderPostsHeading = () => {
                const lab = /^(posts|貼文|帖子|publicaciones|publicações|beiträge|publications)$/i;
                const nodes = Array.from(
                  document.querySelectorAll("span, div, h2, h3, a, [role='tab'], [role='heading']")
                );
                let bestFr = null;
                let bestCount = -1;
                for (const el of nodes) {
                  const raw = clean(el.innerText || el.textContent || "");
                  const aria = clean((el.getAttribute && el.getAttribute("aria-label")) || "");
                  const hit =
                    (lab.test(raw) && raw.length <= 28) || lab.test(aria);
                  if (!hit) continue;
                  let n = el;
                  for (let d = 0; d < 20 && n; d++) {
                    if (n.querySelectorAll) {
                      n.querySelectorAll("[role='feed']").forEach((fr) => {
                        const c = fr.querySelectorAll("div[role='article']").length;
                        if (c > bestCount) {
                          bestCount = c;
                          bestFr = fr;
                        }
                      });
                    }
                    n = n.parentElement;
                  }
                }
                return bestFr;
              };

              let feedRoot = null;
              let bestN = -1;
              document.querySelectorAll("[role='feed']").forEach((fr) => {
                const n = fr.querySelectorAll("div[role='article']").length;
                if (n > bestN) {
                  bestN = n;
                  feedRoot = fr;
                }
              });
              if (!feedRoot) {
                feedRoot =
                  document.querySelector("div[role='feed']") ||
                  document.querySelector("[role='feed']");
              }
              const mainEl = document.querySelector("[role='main']");
              if (mainEl) {
                const n = mainEl.querySelectorAll("div[role='article']").length;
                if (n > bestN) {
                  bestN = n;
                  feedRoot = mainEl;
                }
              }
              const underPostsFeed = findFeedUnderPostsHeading();
              if (underPostsFeed) {
                const nup = underPostsFeed.querySelectorAll("div[role='article']").length;
                if (nup >= 1) feedRoot = underPostsFeed;
              }

              let articles = [];
              if (feedRoot) {
                articles = Array.from(feedRoot.querySelectorAll("div[role='article']"));
              }
              if (!articles.length) {
                articles = Array.from(document.querySelectorAll("div[role='feed'] div[role='article']"));
              }
              if (!articles.length) {
                articles = Array.from(document.querySelectorAll("div[role='article']")).filter((el) =>
                  Array.from(el.querySelectorAll("a[href]")).some((a) =>
                    isPostHref(a.href || "")
                  )
                );
              }

              /**
               * FB often nests the whole feed inside one huge role=article. "topOnly" kept only
               * that wrapper → pickShallowPostAnchor saw a single /posts/ link → fetched=1 and
               * "latest" stuck on an old thread. Prefer leaf articles: each has a /posts/ link and
               * does not strictly contain another candidate that also has its own /posts/ link.
               */
              if (feedRoot && articles.length) {
                const hasShallowPost = (el) => {
                  try {
                    const shell = storyShell(el);
                    return !!pickShallowPostAnchor(shell);
                  } catch (e) {
                    return false;
                  }
                };
                const candidates = articles.filter(hasShallowPost);
                if (candidates.length >= 1) {
                  articles = candidates.filter(
                    (el) => !candidates.some((inner) => inner !== el && el.contains(inner))
                  );
                }
              }

              const scored = [];
              for (const el of articles) {
                const shell = storyShell(el);
                const postA = pickShallowPostAnchor(shell);
                if (!postA) continue;
                const href = canonicalPostUrl(postA.href || "");
                if (!href) continue;
                const body = bodyFromStory(shell);
                if (reactionOnlyBody(body)) continue;
                const r = el.getBoundingClientRect();
                const top = r.top + (window.scrollY || window.pageYOffset || 0);
                scored.push({
                  href,
                  top,
                  text: body,
                  time: timeFromStory(shell, postA),
                  postedUt: postUtFromShell(shell, postA),
                });
              }
              /**
               * Link-first sweep: every /{slug}/posts/… anchor under main/feed. Comet UI often
               * breaks role=article counts; shallowest link in a giant shell is still one URL.
               */
              if (profileSlug && typeof profileSlug === "string" && profileSlug.length) {
                const slugLc = String(profileSlug).toLowerCase();
                /** 优先只在「Posts」下 feed 里扫 /posts/，避免侧栏/推荐里旧链干扰 */
                const scope = underPostsFeed || feedRoot || mainEl || document.body;
                if (scope) {
                  const linkSeen = new Set();
                  for (const a of scope.querySelectorAll("a[href]")) {
                    let h = "";
                    try {
                      const rawH = a.href || "";
                      if (isCommentOrTrackingHref(rawH)) continue;
                      const u = new URL(rawH, location.href);
                      const pn = (u.pathname || "").replace(/\\/+$/, "");
                      const parts = pn.split("/").filter(Boolean);
                      if (
                        parts.length < 3 ||
                        parts[0].toLowerCase() !== slugLc ||
                        parts[1].toLowerCase() !== "posts" ||
                        !parts[2]
                      )
                        continue;
                      h = "https://www.facebook.com" + pn;
                    } catch (e) {
                      continue;
                    }
                    if (linkSeen.has(h)) continue;
                    linkSeen.add(h);

                    let story = null;
                    let el = a;
                    for (let i = 0; i < 30 && el; i++) {
                      if (el.getAttribute && el.getAttribute("role") === "article") {
                        story = el;
                        break;
                      }
                      el = el.parentElement;
                    }
                    if (!story) {
                      el = a;
                      for (let i = 0; i < 18 && el; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        if (
                          el.querySelector &&
                          el.querySelector(
                            "[data-ad-preview='message'], [data-ad-comet-preview='message'], [data-ad-rendering-role='story_message']"
                          )
                        ) {
                          story = el;
                          break;
                        }
                      }
                    }
                    if (!story) story = a.parentElement || a;
                    const shell = storyShell(story);
                    const postA = a;
                    const body = bodyFromStory(shell);
                    if (reactionOnlyBody(body)) continue;
                    const r = a.getBoundingClientRect();
                    const top = r.top + (window.scrollY || window.pageYOffset || 0);
                    scored.push({
                      href: h,
                      top,
                      text: body,
                      time: timeFromStory(shell, postA),
                      postedUt: postUtFromShell(shell, postA),
                    });
                  }
                }
              }

              const byHref = new Map();
              for (const row of scored) {
                const ex = byHref.get(row.href);
                if (!ex) {
                  byHref.set(row.href, row);
                  continue;
                }
                const rNew = reactionOnlyBody(row.text);
                const rOld = reactionOnlyBody(ex.text);
                if (rNew && !rOld) continue;
                if (!rNew && rOld) {
                  byHref.set(row.href, row);
                  continue;
                }
                const ru = row.postedUt || 0;
                const eu = ex.postedUt || 0;
                const lenR = (row.text || "").length;
                const lenE = (ex.text || "").length;
                const topR = row.top != null ? row.top : 1e12;
                const topE = ex.top != null ? ex.top : 1e12;
                if (ru > eu) byHref.set(row.href, row);
                else if (ru === eu) {
                  if (lenR > lenE + 10) byHref.set(row.href, row);
                  else if (lenE > lenR + 10) {
                  } else if (topR < topE) byHref.set(row.href, row);
                }
              }
              const deduped = Array.from(byHref.values());
              /** Feed is newest-first at top after scroll; do NOT put all utime>0 before utime=0 or an old
                  wrong timestamp buries a fresh post with no data-utime (e.g. Mezz ASTR… See more). */
              deduped.sort((a, b) => {
                const ta = a.top != null ? a.top : 1e12;
                const tb = b.top != null ? b.top : 1e12;
                if (ta !== tb) return ta - tb;
                return (b.postedUt || 0) - (a.postedUt || 0);
              });

              const out = [];
              for (const row of deduped.slice(0, 40)) {
                out.push({
                  href: row.href,
                  text: row.text,
                  postedAt: row.time,
                  postedUt: row.postedUt || 0,
                  feedTop: row.top != null ? Math.round(row.top) : null,
                  order: out.length,
                });
              }
              return out;
            }
            """
            profile_slug = self._target_profile_slug()
            try:
                anchors, probe_home = self._collect_profile_feed_anchor_rows(
                    js_profile_feed, profile_slug
                )
            except Exception:
                anchors, probe_home = [], []

            items = self._items_from_profile_feed_evaluate(anchors)
            items = self._merge_dom_with_html_probe(items, probe_home)
            # /{slug}/posts 列表：仅在启用向下滚动兜底时合并（scroll_passes=0 时不跳转，避免与「只视口首条」冲突）
            tab = self._posts_tab_url()
            if tab and int(getattr(self, "_profile_feed_scroll_passes", 11)) > 0:
                try:
                    self._page.goto(tab, wait_until="domcontentloaded", timeout=20000)
                    self._page.wait_for_timeout(1800)
                    anchors2, probe_tab = self._collect_profile_feed_anchor_rows(
                        js_profile_feed, profile_slug
                    )
                    extra = self._items_from_profile_feed_evaluate(anchors2)
                    extra = [
                        replace(p, feed_order=p.feed_order + 8000) for p in extra
                    ]
                    items = self._merge_post_items_by_url(items, extra)
                    items = self._merge_dom_with_html_probe(items, probe_tab)
                except Exception:
                    pass
                try:
                    self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=20000)
                    self._page.wait_for_timeout(800)
                except Exception:
                    pass
            try:
                html_snap = self._page.content()
                probe = self._serialized_html_post_probe(html_snap)
                items = self._merge_dom_with_html_probe(items, probe)
                items = self._reindex_items_by_html_stream_order(items, html_snap)
            except Exception:
                pass
            if items:
                # 先视口截图+OCR 置顶首条，再 permalink enrich（避免 enrich 的 goto 影响「Posts 下第一条」认定）
                items = self._prefer_viewport_first_story_items(items)
                if int(getattr(self, "_profile_feed_scroll_passes", 11)) > 0:
                    items = self._enrich_short_post_bodies_from_permalink(items)
                items = self._finalize_profile_timeline_items(items)
                pl_a, pl_b = 1_000_000, 220_000_000
                items = [
                    replace(p, posted_at="")
                    if pl_a <= int(p.feed_order or 0) < pl_b
                    else p
                    for p in items
                ]
                return self._return_profile_posts_with_visual(items)
            # Fallback on the same page HTML when feed containers are empty.
            html = self._page.content()
            fallback_items = self._extract_posts(html, self.target_url)
            if fallback_items:
                # 走 fallback 时仍要做视口截图+OCR，否则 last_top_post_story_* 永远空，
                # 上层会一直报 "OCR 抓取失败"，等同于看不见新帖。
                fallback_items = self._prefer_viewport_first_story_items(fallback_items)
                return self._return_profile_posts_with_visual(
                    self._finalize_profile_timeline_items(fallback_items)
                )
            raw_fallback = self._finalize_browser_items(
                self._extract_profile_posts_from_raw_html(html)
            )
            if raw_fallback:
                raw_fallback = self._prefer_viewport_first_story_items(raw_fallback)
            return self._return_profile_posts_with_visual(
                self._finalize_profile_timeline_items(raw_fallback)
            )

        js = """
        () => {
          const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
          const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,span,div"))
            .filter(el => {
              const t = (el.textContent || "").trim();
              if (!t || t.length > 80) return false;
              return /listings|在售|出售|商品/i.test(t);
            });

          let root = null;
          for (const h of headings) {
            let node = h;
            for (let i = 0; i < 6 && node; i++) {
              node = node.parentElement;
              if (!node) break;
              const anchors = node.querySelectorAll("a[href*='/marketplace/item/']");
              if (anchors.length >= 1 && anchors.length <= 80) {
                root = node;
                break;
              }
            }
            if (root) break;
          }

          const out = [];
          const seen = new Set();

          if (root) {
            for (const a of Array.from(root.querySelectorAll("a[href*='/marketplace/item/']"))) {
              const href = a.href || "";
              if (!href || seen.has(href)) continue;
              seen.add(href);
              out.push({ href, text: clean(a.textContent || "") });
            }
          }

          // Add links that appear in pending/review context (e.g. "审核中", "pending")
          for (const a of Array.from(document.querySelectorAll("a[href*='/marketplace/item/']"))) {
            const href = a.href || "";
            if (!href || seen.has(href)) continue;
            const p = a.parentElement;
            const context = clean((p?.innerText || p?.textContent || "") + " " + (a.innerText || a.textContent || ""));
            if (!/(审核中|審核中|pending|in review|under review)/i.test(context)) {
              continue;
            }
            seen.add(href);
            out.push({ href, text: clean(a.textContent || "") || "Marketplace listing" });
          }

          return out;
        }
        """
        anchors_map: dict[str, str] = {}
        for _ in range(5):
            anchors = self._page.evaluate(js)
            for anchor in anchors:
                href = (anchor.get("href") or "").strip()
                text = (anchor.get("text") or "").strip()
                if href:
                    anchors_map[href] = text
            # Scroll to load more listings in profile section.
            self._page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9));")
            self._page.wait_for_timeout(800)
            if len(anchors_map) >= 30:
                break

        items: list[PostItem] = []
        seen_urls: set[str] = set()

        for href, text in anchors_map.items():
            if not href:
                continue
            normalized = self._normalize_url(href)
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            clean_text = text.strip() if text else "Marketplace listing"
            items.append(self._to_post_item(normalized, clean_text))

        detail_by_url = self._fetch_item_detail_map([item.url for item in items], max_items=30)
        if detail_by_url:
            items = [
                PostItem(
                    post_id=item.post_id,
                    url=item.url,
                    text=item.text,
                    detail=detail_by_url.get(item.url, ""),
                    posted_at=item.posted_at,
                    posted_utime=item.posted_utime,
                    feed_order=item.feed_order,
                )
                for item in items
            ]

        return self._finalize_browser_items(items)

    @staticmethod
    def _is_playwright_browser_missing_error(exc: BaseException) -> bool:
        s = str(exc).lower()
        return (
            "executable doesn't exist" in s
            or "chromium_headless_shell" in s
            or ("playwright install" in s and "browser" in s)
        )

    @staticmethod
    def _install_playwright_chromium_browsers() -> None:
        """无头模式需要 chromium-headless-shell；只装 chromium 仍会报 Executable doesn't exist。"""
        cmd = [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            "chromium-headless-shell",
        ]
        try:
            subprocess.run(
                cmd,
                check=False,
                timeout=600,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass

    def _ensure_browser(self) -> None:
        if self._page is not None:
            return
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright is required for marketplace pages. "
                "Run: pip install playwright && "
                "python3 -m playwright install chromium chromium-headless-shell"
            )

        cls = self.__class__
        locale = (self.accept_language.split(",")[0] or "en-US").strip() or "en-US"
        _pc_args = dict(
            user_agent=USER_AGENT,
            locale=locale,
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,
            args=["--disable-blink-features=AutomationControlled"],
        )

        if self.browser_user_data_dir:
            if cls._shared_playwright is None:
                cls._shared_playwright = sync_playwright().start()
            if cls._shared_persistent_context is None:
                try:
                    cls._shared_persistent_context = (
                        cls._shared_playwright.chromium.launch_persistent_context(
                            self.browser_user_data_dir,
                            headless=self.headless,
                            **_pc_args,
                        )
                    )
                except Exception as e:
                    if not self._is_playwright_browser_missing_error(e):
                        raise
                    self._install_playwright_chromium_browsers()
                    cls._shared_persistent_context = (
                        cls._shared_playwright.chromium.launch_persistent_context(
                            self.browser_user_data_dir,
                            headless=self.headless,
                            **_pc_args,
                        )
                    )
            self._context = cls._shared_persistent_context
            self._uses_persistent_context = True
            cookies = self._parse_cookie_string(self.cookie)
            if cookies:
                self._context.add_cookies(cookies)
            self._page = self._context.new_page()
            try:
                self._page.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "window.chrome=window.chrome||{runtime:{}};"
                )
            except Exception:
                pass
            cls._persistent_context_users += 1
            return

        if cls._shared_browser is None:
            if cls._shared_playwright is None:
                cls._shared_playwright = sync_playwright().start()
            try:
                cls._shared_browser = cls._shared_playwright.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as e:
                if not self._is_playwright_browser_missing_error(e):
                    raise
                self._install_playwright_chromium_browsers()
                cls._shared_browser = cls._shared_playwright.chromium.launch(
                    headless=self.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
        self._context = cls._shared_browser.new_context(
            user_agent=USER_AGENT,
            locale=locale,
            viewport={"width": 1280, "height": 900},
            device_scale_factor=2,
        )
        cls._shared_context_users += 1
        cookies = self._parse_cookie_string(self.cookie)
        if cookies:
            self._context.add_cookies(cookies)
        self._page = self._context.new_page()
        try:
            self._page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome=window.chrome||{runtime:{}};"
            )
        except Exception:
            pass

    def _fetch_item_detail_map(self, urls: list[str], max_items: int = 30) -> dict[str, str]:
        if not self._context or not urls:
            return {}
        result: dict[str, str] = {}
        detail_page = self._context.new_page()
        try:
            for url in urls[:max_items]:
                try:
                    detail_page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    detail_page.wait_for_timeout(600)
                    dom_meta = detail_page.evaluate(
                        """
                        () => {
                          const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
                          const h1 = document.querySelector("h1");
                          const title = clean(
                            h1?.textContent ||
                            document.querySelector("meta[property='og:title']")?.content ||
                            document.title
                          ).replace(/^Marketplace\\s*-\\s*/i, "");

                          // Scope only the listing detail panel near H1 to avoid cross-item contamination.
                          let panel = h1;
                          for (let i = 0; i < 2 && panel; i++) {
                            panel = panel.parentElement;
                          }
                          if (!panel) {
                            panel = document.querySelector("main") || document.body;
                          }
                          const panelText = clean(panel.innerText || panel.textContent || "");

                          const priceMatch = panelText.match(/(CA\\$|US\\$|\\$|€|£)\\s*[0-9][0-9,\\.]{0,20}/);
                          const price = priceMatch ? clean(priceMatch[0]) : "";

                          let desc = panelText;
                          if (title) desc = desc.replace(title, " ");
                          if (price) desc = desc.replace(price, " ");
                          desc = desc.replace(/\\b(Like|Share|Edit|Details|Condition|Listed|Location is approximate|Seller information|Seller details)\\b/gi, " ");
                          desc = clean(desc).slice(0, 320);

                          return { title, price, desc };
                        }
                        """
                    )
                    html = detail_page.content()
                    regex_payload = self._extract_item_snapshot_from_html(url, html)
                    title = (dom_meta.get("title") or "").strip()
                    price = (dom_meta.get("price") or "").strip()
                    desc = (dom_meta.get("desc") or "").strip()
                    dom_payload = " | ".join(x for x in [title, price, desc] if x).strip()
                    payload = dom_payload if desc else (dom_payload or regex_payload)
                    if payload:
                        result[url] = payload[:600]
                except Exception:
                    continue
        finally:
            try:
                detail_page.close()
            except Exception:
                pass
        return result

    @staticmethod
    def _extract_item_snapshot_from_html(url: str, html: str) -> str:
        item_match = re.search(r"/marketplace/item/(\d+)", url)
        if not item_match:
            return ""
        item_id = item_match.group(1)
        idx = html.find(f"\"id\":\"{item_id}\"")
        window = html if idx < 0 else html[max(0, idx - 2000) : min(len(html), idx + 14000)]

        title = FacebookPostMonitor._first_match(
            window,
            [
                r"\"marketplace_listing_title\":\"([^\"]+)\"",
                r"\"base_marketplace_listing_title\":\"([^\"]+)\"",
            ],
        )
        price = FacebookPostMonitor._first_match(
            window,
            [
                r"\"formatted_amount_zeros_stripped\":\"([^\"]+)\"",
                r"\"amount\":\"([0-9]+(?:\\.[0-9]+)?)\"",
            ],
        )
        if price and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", price):
            price = f"${price}"

        desc = FacebookPostMonitor._first_match(
            window,
            [
                r"\"marketplace_listing_description\":\"([^\"]{10,1600})\"",
                r"\"redacted_description\":\"([^\"]{10,1600})\"",
                r"\"description_text\":\"([^\"]{10,1600})\"",
            ],
        )

        blacklist = ("show all comments", "potential spam", "notifications", "browse all")
        desc_l = desc.lower()
        if any(x in desc_l for x in blacklist):
            desc = ""

        parts = [p for p in [title, price, desc] if p]
        payload = " | ".join(parts)
        payload = re.sub(r"\s+", " ", payload).strip()
        return payload

    @staticmethod
    def _first_match(text: str, patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                value = match.group(1).replace("\\/", "/")
                value = value.replace("\\u0025", "%")
                value = re.sub(r"\\u[0-9a-fA-F]{4}", "", value)
                value = re.sub(r"\s+", " ", value).strip()
                return value
        return ""

    @staticmethod
    def _parse_cookie_string(cookie_header: str) -> list[dict]:
        if not cookie_header:
            return []
        cookies: list[dict] = []
        parts = [part.strip() for part in cookie_header.split(";") if "=" in part]
        for part in parts:
            name, value = part.split("=", 1)
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".facebook.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                }
            )
        return cookies

    def _extract_posts(self, html: str, base_url: str) -> list[PostItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[PostItem] = []
        seen_urls: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not href:
                continue

            abs_url = urljoin(base_url, href)

            normalized = self._normalize_url(abs_url)
            if self._has_profile_allowlist():
                if not self._passes_profile_allowlist(normalized):
                    continue
            elif not self._looks_like_post_url(abs_url, marketplace=self.is_marketplace_target):
                continue
            if not self._is_in_target_scope(normalized):
                continue
            if not self._is_canonical_timeline_post_url(normalized):
                continue
            if normalized in seen_urls:
                continue
            if not self._passes_profile_allowlist(normalized):
                continue
            seen_urls.add(normalized)

            text = anchor.get_text(" ", strip=True)
            items.append(self._to_post_item(normalized, text))

        # Marketplace pages often embed item links in JSON/script blocks.
        if self.is_marketplace_target:
            for normalized in self._extract_marketplace_links_from_raw_html(html, base_url):
                if normalized in seen_urls:
                    continue
                seen_urls.add(normalized)
                items.append(self._to_post_item(normalized, "Marketplace listing"))

        return items

    def _extract_profile_posts_from_raw_html(self, html: str) -> list[PostItem]:
        # Strict fallback for profile feeds: only reconstruct canonical /posts/pfbid* URLs.
        profile_slug = urlparse(self.target_url).path.strip("/").split("/", 1)[0]
        if not profile_slug:
            return []
        pfbids = sorted(set(re.findall(r"pfbid[0-9A-Za-z]+", html)))
        items: list[PostItem] = []
        for pfbid in pfbids[:20]:
            url = f"https://www.facebook.com/{profile_slug}/posts/{pfbid}"
            items.append(self._to_post_item(url, "Facebook post"))
        return items

    @staticmethod
    def _to_post_item(
        url: str,
        text: str,
        detail: str = "",
        posted_at: str = "",
        posted_utime: int = 0,
        feed_order: int = 999999,
    ) -> PostItem:
        post_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return PostItem(
            post_id=post_id,
            url=url,
            text=text,
            detail=detail,
            posted_at=posted_at,
            posted_utime=posted_utime,
            feed_order=feed_order,
        )

    @staticmethod
    def _extract_marketplace_links_from_raw_html(html: str, base_url: str) -> list[str]:
        patterns = (
            r"https?://www\.facebook\.com/marketplace/item/\d+",
            r"/marketplace/item/\d+",
            r"https?:\\/\\/www\.facebook\.com\\/marketplace\\/item\\/\d+",
            r"\\/marketplace\\/item\\/\d+",
        )
        found: set[str] = set()
        for pattern in patterns:
            for match in re.findall(pattern, html):
                candidate = match.replace("\\/", "/")
                normalized = FacebookPostMonitor._normalize_url(urljoin(base_url, candidate))
                if FacebookPostMonitor._looks_like_post_url(normalized):
                    found.add(normalized)
        return list(found)

    @staticmethod
    def _looks_like_post_url(url: str, marketplace: bool = False) -> bool:
        if marketplace:
            return "/marketplace/item/" in url
        if "/posts/" in url:
            return True
        return False

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        marketplace_match = re.search(r"/marketplace/item/(\d+)", parsed.path)
        if marketplace_match:
            item_id = marketplace_match.group(1)
            return f"https://www.facebook.com/marketplace/item/{item_id}"
        path = parsed.path or ""
        qs = parse_qs(parsed.query, keep_blank_values=False)

        # Stable canonical forms for common Facebook post link types.
        if path.startswith("/photo/") and qs.get("fbid"):
            return f"https://www.facebook.com/photo/?fbid={qs['fbid'][0]}"
        if "/posts/" in path:
            clean_path = path.rstrip("/")
            return f"https://www.facebook.com{clean_path}"
        if "/permalink/" in path:
            clean_path = path.rstrip("/")
            return f"https://www.facebook.com{clean_path}"
        if path.endswith("/story.php") and qs.get("story_fbid"):
            story_fbid = qs["story_fbid"][0]
            member_id = qs.get("id", [""])[0]
            if member_id:
                return f"https://www.facebook.com/story.php?story_fbid={story_fbid}&id={member_id}"
            return f"https://www.facebook.com/story.php?story_fbid={story_fbid}"

        # Fallback: drop volatile query parameters for stable identity.
        clean = parsed._replace(query="", fragment="")
        return clean.geturl()
