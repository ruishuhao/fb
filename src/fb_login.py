"""One-shot: log into Facebook in a visible browser; session is saved under FB_BROWSER_USER_DATA_DIR.

Supports two modes:
  - Auto: if FB_EMAIL + FB_PASSWORD are set in .env, fills the form automatically.
  - Manual: opens Chromium and waits for you to log in by hand.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.config import _project_root, _resolve_under_project
from src.fb_monitor import USER_AGENT


def main() -> None:
    load_dotenv(_project_root() / ".env", override=True)
    raw = (os.getenv("FB_BROWSER_USER_DATA_DIR") or "").strip() or ".fb-browser-profile"
    user_data = _resolve_under_project(raw)
    fb_email = os.getenv("FB_EMAIL", "").strip()
    fb_password = os.getenv("FB_PASSWORD", "").strip()

    print(f"Using profile directory:\n  {user_data}")
    if fb_email:
        print(f"Auto-login with: {fb_email[:4]}***")
    else:
        print("No FB_EMAIL set — manual login mode")
    print()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data,
            headless=False,
            user_agent=USER_AGENT,
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.new_page()
            page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            if fb_email and fb_password:
                try:
                    page.fill('input#email, input[name="email"]', fb_email)
                    page.fill('input#pass, input[name="pass"]', fb_password)
                    page.click('button[name="login"], button[data-testid="royal_login_button"], button[type="submit"]')
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.wait_for_timeout(4000)
                    url = (page.url or "").lower()
                    if "checkpoint" in url or "two_step" in url:
                        print("2FA/checkpoint detected — please complete verification in the browser.")
                        input("Press Enter here after verification is complete… ")
                    elif "login" in url:
                        print("Still on login page — credentials may be wrong. Complete login manually.")
                        input("Press Enter here after login is complete… ")
                    else:
                        print("Login succeeded!")
                        page.wait_for_timeout(2000)
                except Exception as exc:
                    print(f"Auto-fill failed: {exc}")
                    print("Please log in manually in the browser.")
                    input("Press Enter here after login is complete… ")
            else:
                input("Please log in manually, then press Enter here… ")
        finally:
            ctx.close()

    print("Session saved. You can now start the watcher.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)
