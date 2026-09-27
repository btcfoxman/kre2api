"""Use Krea's own sign-in page so its Turnstile and session cookies stay together."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


class KreaLoginError(Exception):
    def __init__(self, message: str, status: str = "login_failed") -> None:
        super().__init__(message)
        self.status = status


def browser_proxy(proxy_url: str) -> dict[str, str] | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
        raise ValueError("proxy must use http://, https:// or socks5:// with host and port")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def _click_login(page) -> None:
    for pattern in (r"log in|sign in|登录", r"continue with email|email|邮箱"):
        button = page.get_by_role("button", name=re.compile(pattern, re.I))
        for index in range(min(button.count(), 5)):
            item = button.nth(index)
            if item.is_visible():
                item.click(timeout=3000)
                return


def _billing(page) -> dict | None:
    try:
        result = page.evaluate("""async () => {
          const response = await fetch('/api/billing-data', {credentials:'include'});
          if (!response.ok) return null;
          return await response.json();
        }""")
        return result if isinstance(result, dict) and "balance" in result else None
    except Exception:
        return None


def _project(page) -> str:
    try:
        result = page.evaluate("""async () => {
          const response = await fetch('/api/sessions', {credentials:'include'});
          return response.ok ? await response.json() : [];
        }""")
        sessions = result if isinstance(result, list) else result.get("sessions", [])
        videos = [item for item in sessions if isinstance(item, dict)
                  and item.get("tool") == "videoV2" and item.get("id")]
        videos.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return str(videos[0]["id"]) if videos else ""
    except Exception:
        return ""


def login(account_id: int, email: str, password: str, proxy_url: str,
          data_dir: Path) -> tuple[list[dict], str, str, float]:
    profile = data_dir / "kr-chrome-profiles" / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    timeout = max(30, min(int(os.getenv("KR_LOGIN_TIMEOUT_SECONDS", "120")), 300))
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), executable_path=os.getenv("KR_CHROMIUM_PATH", "/usr/bin/chromium"),
                headless=os.getenv("KR_CHROME_HEADLESS", "0") == "1",
                proxy=browser_proxy(proxy_url), viewport={"width": 1280, "height": 960},
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.krea.ai/video", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                state = _billing(page)
                if state is None:
                    deadline = time.monotonic() + timeout
                    submitted = False
                    while time.monotonic() < deadline:
                        email_input = page.locator("input[type='email'], input[autocomplete='email']").first
                        password_input = page.locator("input[type='password']").first
                        if email_input.count() and email_input.is_visible() and not submitted:
                            email_input.fill(email)
                            if not (password_input.count() and password_input.is_visible()):
                                try:
                                    page.get_by_role("dialog").get_by_role(
                                        "button", name="Continue", exact=True).click(timeout=10000)
                                except PlaywrightTimeoutError as exc:
                                    raise KreaLoginError("Krea email step needs browser verification", "challenge_required") from exc
                            submitted = False
                        if password_input.count() and password_input.is_visible() and not submitted:
                            password_input.fill(password)
                            try:
                                page.get_by_role("dialog").get_by_role(
                                    "button", name="Continue", exact=True).click(timeout=15000)
                            except PlaywrightTimeoutError as exc:
                                raise KreaLoginError("Krea password step needs browser verification", "challenge_required") from exc
                            submitted = True
                        elif not email_input.count() and not password_input.count():
                            _click_login(page)
                        page.wait_for_timeout(2000)
                        state = _billing(page)
                        if state is not None:
                            break
                        visible_text = page.locator("body").inner_text(timeout=3000).lower()
                        if any(term in visible_text for term in
                               ("invalid login credentials", "incorrect password", "密码错误")):
                            raise KreaLoginError("Krea rejected the email or password")
                    if state is None:
                        raise KreaLoginError("Krea login needs browser verification or timed out", "challenge_required")
                cookies = [item for item in context.cookies("https://www.krea.ai")
                           if str(item.get("domain") or "").endswith("krea.ai")]
                if not any(item.get("name") == "sb-superb-auth-token" for item in cookies):
                    raise KreaLoginError("Krea session cookie was not issued", "challenge_required")
                balance = float((state.get("balance") or {}).get("total") or 0)
                return cookies, str(page.evaluate("navigator.userAgent")), _project(page), balance
            finally:
                context.close()
    except KreaLoginError:
        raise
    except Exception as exc:
        raise KreaLoginError(f"Krea browser login failed: {type(exc).__name__}", "network_error") from exc
