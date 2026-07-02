#!/usr/bin/env python3
"""Playwright 打开主页 → 主列首条 story 动截图 + DOM + OCR（不写死预期文案）。

单次人工对照（可选）：
  .venv/bin/python scripts/fb_ocr_latest_post.py --contains "Mezz ASTR"
  仅打印 PASS/FAIL，不写 .env，也不影响以后轮询。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

load_dotenv(_ROOT / ".env")

from src.config import load_config
from src.fb_monitor import FacebookPostMonitor


def main() -> None:
    ap = argparse.ArgumentParser(description="Live capture first visible profile story.")
    ap.add_argument(
        "--contains",
        metavar="SUBSTRING",
        help="单次校验：DOM 或 OCR 是否包含该子串（不写配置文件）",
    )
    ap.add_argument(
        "--extra",
        type=int,
        metavar="N",
        help="使用 EXTRA_TARGET_URLS 中第 N 个（0 起）",
    )
    args = ap.parse_args()

    cfg = load_config()
    url = (cfg.target_url or "").strip()
    if not url and cfg.extra_target_urls:
        url = cfg.extra_target_urls[0].strip()
    if not url:
        print("错误：.env 中未设置 TARGET_URL 或 EXTRA_TARGET_URLS", file=sys.stderr)
        sys.exit(2)
    if args.extra is not None and cfg.extra_target_urls:
        if 0 <= args.extra < len(cfg.extra_target_urls):
            url = cfg.extra_target_urls[args.extra].strip()
    out_png = _ROOT / "logs" / "fb_latest_post_story.png"
    mon = FacebookPostMonitor(
        url,
        timeout_seconds=max(120, cfg.request_timeout_seconds),
        cookie=cfg.facebook_cookie,
        accept_language=cfg.fb_accept_language,
        browser_user_data_dir=cfg.fb_browser_user_data_dir,
        browser_headless=cfg.fb_browser_headless,
        extra_feed_ocr_lang=cfg.extra_feed_ocr_lang,
    )
    try:
        try:
            best = mon.read_latest_post_story_via_browser_ocr(screenshot_path=str(out_png))
        except Exception as e:
            err = str(e).lower()
            if "executable doesn't exist" in err or "playwright install" in err:
                print(
                    "Playwright 尚未下载浏览器（无头模式需要 chromium-headless-shell）。执行：\n"
                    "  .venv/bin/python -m playwright install chromium chromium-headless-shell\n"
                    "或: bash scripts/install_playwright_browsers.sh",
                    file=sys.stderr,
                )
            raise
        print("post_url:", mon.last_top_post_story_post_url or "(none)")
        print("screenshot:", mon.last_top_post_story_screenshot_path or str(out_png))
        print("--- dom (首条 story 正文) ---")
        print(mon.last_top_post_story_dom_text or "(empty)")
        print("--- ocr ---")
        print(mon.last_top_post_story_ocr_text or "(empty)")
        print("--- best (dom 优先) ---")
        print(best or "(empty)")
        needle = (args.contains or "").strip()
        if needle:
            dom = (mon.last_top_post_story_dom_text or "").casefold()
            ocr = (mon.last_top_post_story_ocr_text or "").casefold()
            ok = needle.casefold() in dom or needle.casefold() in ocr
            print("ONE_SHOT_VERIFY:", "PASS" if ok else "FAIL", repr(needle[:120]))
    finally:
        mon.close()


if __name__ == "__main__":
    main()
