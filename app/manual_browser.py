"""Admin assisted login in the account's own native Chromium profile."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from .browser_login import (
    KreaLoginError, _CDP, _account_email, _login_cdp, _profile_guard,
    _remove_stale_singletons, _targets, browser_proxy,
)


class ManualBrowser:
    def __init__(self, account_id: int, proxy_url: str, data_dir: Path) -> None:
        self.account_id = account_id
        self.port = 19400 + account_id
        if self.port > 65535:
            raise KreaLoginError("Account ID exceeds the browser port range")
        self.lock = threading.RLock()
        self.profile = data_dir / "kr-native-profiles" / str(account_id)
        self.profile.mkdir(parents=True, exist_ok=True)
        self.guard = _profile_guard(self.profile)
        self.guard.__enter__()
        self.process: subprocess.Popen | None = None
        try:
            _remove_stale_singletons(self.profile)
            chrome = os.getenv("KR_CHROMIUM_PATH", "") or shutil.which("chromium")
            if not chrome:
                raise KreaLoginError("Chromium executable is unavailable", "network_error")
            command = [chrome, "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                       "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
                       f"--remote-debugging-port={self.port}", "--remote-allow-origins=*",
                       f"--user-data-dir={self.profile}", "--window-size=1280,960"]
            if proxy := browser_proxy(proxy_url):
                command.append(f"--proxy-server={proxy}")
            command.append("https://www.krea.ai/video")
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    self.target()
                    break
                except (OSError, ValueError, KreaLoginError):
                    time.sleep(0.5)
            else:
                raise KreaLoginError("Krea browser did not open", "network_error")
        except Exception:
            self.close()
            raise

    def target(self) -> dict:
        for item in _targets(self.port):
            if item.get("type") == "page" and urlsplit(str(item.get("url") or "")).hostname == "www.krea.ai":
                return item
        raise KreaLoginError("Krea browser page is unavailable", "network_error")

    async def _connect(self):
        return await websockets.connect(self.target()["webSocketDebuggerUrl"],
                                        origin=None, proxy=None, open_timeout=10)

    async def _snapshot(self) -> dict:
        async with await self._connect() as socket:
            cdp = _CDP(socket)
            shot = await cdp.call("Page.captureScreenshot", {
                "format": "jpeg", "quality": 78, "captureBeyondViewport": False,
            })
            state = await cdp.evaluate("""(() => ({
              url: location.origin + location.pathname,
              has_login_form: !!document.querySelector('input[type=email],input[type=password]'),
              challenge_required: [...document.querySelectorAll('iframe')].some(x =>
                x.src.includes('challenges.cloudflare.com') && x.getBoundingClientRect().width > 20) ||
                /verify you are human|troubleshoot/i.test(document.body?.innerText || '')
            }))()""")
            cookies = (await cdp.call("Network.getCookies", {
                "urls": ["https://www.krea.ai/video"]})).get("cookies", [])
            return {"image": "data:image/jpeg;base64," + str(shot.get("data") or ""),
                    "url": state.get("url") if isinstance(state, dict) else "https://www.krea.ai/video",
                    "has_login_form": bool(state.get("has_login_form")) if isinstance(state, dict) else False,
                    "challenge_required": bool(state.get("challenge_required")) if isinstance(state, dict) else False,
                    "signed_in": bool(_account_email(cookies))}

    def snapshot(self) -> dict:
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise KreaLoginError("Krea browser closed", "network_error")
            return asyncio.run(self._snapshot())

    async def _action(self, action: dict) -> dict:
        async with await self._connect() as socket:
            cdp = _CDP(socket)
            kind = action.get("action")
            if kind == "type":
                value = action.get("text")
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ValueError("text must contain 1-128 characters")
                await cdp.call("Input.insertText", {"text": value})
            elif kind == "enter":
                for event in ("keyDown", "keyUp"):
                    await cdp.call("Input.dispatchKeyEvent", {
                        "type": event, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13,
                    })
            elif kind in {"click", "scroll"}:
                x = float(action.get("x", 0.5))
                y = float(action.get("y", 0.5))
                if not 0 <= x <= 1 or not 0 <= y <= 1:
                    raise ValueError("browser coordinates must be between 0 and 1")
                viewport = await cdp.evaluate("({width:innerWidth,height:innerHeight})")
                point = {"x": min(x * viewport["width"], viewport["width"] - 1),
                         "y": min(y * viewport["height"], viewport["height"] - 1)}
                if kind == "click":
                    for event in ("mouseMoved", "mousePressed", "mouseReleased"):
                        params = {"type": event, **point}
                        if event != "mouseMoved":
                            params.update(button="left", clickCount=1)
                        await cdp.call("Input.dispatchMouseEvent", params)
                else:
                    await cdp.call("Input.dispatchMouseEvent", {
                        "type": "mouseWheel", **point, "deltaX": 0,
                        "deltaY": max(-1000, min(1000, float(action.get("delta_y", 0)))),
                    })
            else:
                raise ValueError("unsupported browser action")
            return {"phase": kind}

    def action(self, action: dict, email: str = "", password: str = "") -> dict:
        with self.lock:
            if action.get("action") == "login":
                if not email or not password:
                    raise ValueError("account email and password are required")
                try:
                    asyncio.run(_login_cdp(self.port, email, password, 35))
                    return {"phase": "signed_in"}
                except KreaLoginError as exc:
                    if exc.status == "challenge_required":
                        return {"phase": "challenge_required"}
                    raise
            return asyncio.run(self._action(action))

    async def _capture(self, expected_email: str) -> tuple[list[dict], str, str]:
        async with await self._connect() as socket:
            cdp = _CDP(socket)
            cookies = (await cdp.call("Network.getCookies", {
                "urls": ["https://www.krea.ai/video"]})).get("cookies", [])
            if _account_email(cookies) != expected_email.lower():
                raise KreaLoginError("Complete Krea login in this account's browser first",
                                     "challenge_required")
            user_agent = str(await cdp.evaluate("navigator.userAgent") or "")
            sessions = await cdp.evaluate("""(async () => {
              const r = await fetch('/api/sessions', {credentials:'include'});
              return r.ok ? await r.json() : [];
            })()""", promise=True)
            if isinstance(sessions, dict):
                sessions = sessions.get("sessions", [])
            videos = [item for item in sessions if isinstance(item, dict)
                      and item.get("tool") == "videoV2" and item.get("id")]
            videos.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return cookies, user_agent, str(videos[0]["id"]) if videos else ""

    def capture(self, expected_email: str) -> tuple[list[dict], str, str]:
        with self.lock:
            return asyncio.run(self._capture(expected_email))

    def close(self) -> None:
        with self.lock:
            if self.process:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                self.process = None
            if self.guard:
                self.guard.__exit__(None, None, None)
                self.guard = None
