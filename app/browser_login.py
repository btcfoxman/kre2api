"""Native Chromium login through CDP, with a persistent profile per account."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import websockets


class KreaLoginError(Exception):
    def __init__(self, message: str, status: str = "login_failed") -> None:
        super().__init__(message)
        self.status = status


def browser_proxy(proxy_url: str) -> str:
    if proxy_url:
        parsed = urlsplit(proxy_url)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
            raise ValueError("proxy must use http://, https:// or socks5:// with host and port")
    return proxy_url


def _targets(port: int) -> list[dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
        return json.load(response)


def _account_email(cookies: list[dict]) -> str:
    token = next((str(item.get("value") or "") for item in cookies
                  if item.get("name") == "sb-superb-auth-token"), "")
    if not token:
        return ""
    try:
        raw = token.removeprefix("base64-")
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return ""

    def find(value: object) -> str:
        if isinstance(value, dict):
            if isinstance(value.get("email"), str):
                return value["email"].lower()
            return next((found for item in value.values() if (found := find(item))), "")
        if isinstance(value, list):
            return next((found for item in value if (found := find(item))), "")
        return ""

    return find(payload)


class _CDP:
    def __init__(self, socket: object) -> None:
        self.socket = socket
        self.sequence = 0

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.sequence += 1
        identifier = self.sequence
        await self.socket.send(json.dumps({"id": identifier, "method": method,
                                           "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.socket.recv(), timeout=20))
            if message.get("id") != identifier:
                continue
            if message.get("error"):
                raise RuntimeError(f"CDP {method} failed")
            return message.get("result") or {}

    async def evaluate(self, expression: str, *, promise: bool = False) -> object:
        result = await self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": promise,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError("Krea page script failed")
        return result.get("result", {}).get("value")

    async def click(self, selector: str, label: str = "") -> bool:
        expression = """(() => {
          const node = [...document.querySelectorAll(%s)].find(x =>
            (!%s || x.innerText.trim() === %s) && !x.disabled &&
            x.getBoundingClientRect().width > 0);
          if (!node) return null;
          const r = node.getBoundingClientRect();
          return {x:r.x+r.width/2, y:r.y+r.height/2};
        })()""" % (json.dumps(selector), json.dumps(label), json.dumps(label))
        position = await self.evaluate(expression)
        if not isinstance(position, dict):
            return False
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": event, **position}
            if event != "mouseMoved":
                params.update(button="left", clickCount=1)
            await self.call("Input.dispatchMouseEvent", params)
        return True

    async def fill(self, selector: str, value: str) -> bool:
        if not await self.click(selector):
            return False
        await self.call("Input.insertText", {"text": value})
        return True

    async def challenge_click(self) -> bool:
        position = await self.evaluate("""(() => {
          const frame = [...document.querySelectorAll('iframe')].find(x => {
            const r = x.getBoundingClientRect();
            return r.width > 40 && r.height > 30 &&
              (x.src.includes('challenges.cloudflare.com') ||
               x.title.toLowerCase().includes('challenge'));
          });
          if (!frame) return null;
          const r = frame.getBoundingClientRect();
          return {x:r.x+Math.min(30,r.width/3), y:r.y+r.height/2};
        })()""")
        if not isinstance(position, dict):
            return False
        for event in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": event, **position}
            if event != "mouseMoved":
                params.update(button="left", clickCount=1)
            await self.call("Input.dispatchMouseEvent", params)
        return True


async def _login_cdp(port: int, email: str, password: str, timeout: int
                     ) -> tuple[list[dict], str, str, float]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            page = next(item for item in _targets(port) if item.get("type") == "page"
                        and urlsplit(str(item.get("url") or "")).hostname == "www.krea.ai")
            break
        except (OSError, StopIteration, ValueError):
            if time.monotonic() >= deadline:
                raise KreaLoginError("Native Chromium did not open Krea", "network_error")
            await asyncio.sleep(0.5)
    async with websockets.connect(page["webSocketDebuggerUrl"], origin=None,
                                  proxy=None, open_timeout=10) as socket:
        cdp = _CDP(socket)
        email_filled = email_submitted = password_filled = submitted = challenge_clicked = False
        await asyncio.sleep(1)
        while time.monotonic() < deadline:
            state = await cdp.evaluate("""(async () => {
              try {
                const r = await fetch('/api/billing-data', {credentials:'include'});
                if (!r.ok) return null;
                const body = await r.json();
                return body && typeof body.balance === 'object' ? body : null;
              } catch (_) { return null; }
            })()""", promise=True)
            if isinstance(state, dict):
                cookies = (await cdp.call("Network.getCookies", {
                    "urls": ["https://www.krea.ai/video"]})).get("cookies", [])
                actual_email = _account_email(cookies)
                if actual_email and actual_email != email.lower():
                    raise KreaLoginError("Browser session belongs to a different Krea account")
                if not actual_email:
                    raise KreaLoginError("Krea session identity could not be verified", "challenge_required")
                user_agent = str(await cdp.evaluate("navigator.userAgent") or "")
                sessions = await cdp.evaluate("""(async () => {
                  try {
                    const r = await fetch('/api/sessions', {credentials:'include'});
                    return r.ok ? await r.json() : [];
                  } catch (_) { return []; }
                })()""", promise=True)
                if isinstance(sessions, dict):
                    sessions = sessions.get("sessions", [])
                videos = [item for item in sessions if isinstance(item, dict)
                          and item.get("tool") == "videoV2" and item.get("id")]
                videos.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
                project = str(videos[0]["id"]) if videos else ""
                return cookies, user_agent, project, float((state["balance"] or {}).get("total") or 0)

            form = await cdp.evaluate("""({
              email: !!document.querySelector('input[type=email]'),
              password: !!document.querySelector('input[type=password]'),
              continueEnabled: [...document.querySelectorAll('[role=dialog] button')]
                .some(x => x.innerText.trim() === 'Continue' && !x.disabled),
              error: (document.querySelector('[role=dialog]')?.innerText || '').toLowerCase()
            })""")
            if not isinstance(form, dict):
                await asyncio.sleep(1)
                continue
            if any(term in form["error"] for term in
                   ("invalid login credentials", "incorrect password", "wrong password")):
                raise KreaLoginError("Krea rejected the email or password")
            if form["password"] and not password_filled:
                password_filled = await cdp.fill("input[type=password]", password)
            elif form["email"] and not email_filled:
                email_filled = await cdp.fill("input[type=email]", email)
            elif form["continueEnabled"] and form["password"] and not submitted:
                submitted = await cdp.click("[role=dialog] button", "Continue")
            elif form["continueEnabled"] and form["email"] and not email_submitted:
                email_submitted = await cdp.click("[role=dialog] button", "Continue")
            elif not form["email"] and not form["password"]:
                await cdp.click("button", "Sign in")
            elif password_filled and not form["continueEnabled"] and not challenge_clicked:
                challenge_clicked = await cdp.challenge_click()
            await asyncio.sleep(1.5)
        raise KreaLoginError("Krea login requires browser verification or timed out",
                             "challenge_required")


def login(account_id: int, email: str, password: str, proxy_url: str,
          data_dir: Path) -> tuple[list[dict], str, str, float]:
    profile = data_dir / "kr-native-profiles" / str(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    timeout = max(30, min(int(os.getenv("KR_LOGIN_TIMEOUT_SECONDS", "120")), 300))
    port = 19400 + account_id
    if port > 65535:
        raise KreaLoginError("Account ID exceeds the browser port range")
    chrome = os.getenv("KR_CHROMIUM_PATH", "") or shutil.which("chromium")
    if not chrome:
        raise KreaLoginError("Chromium executable is unavailable", "network_error")
    proxy = browser_proxy(proxy_url)
    command = [chrome, "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
               "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
               f"--remote-debugging-port={port}", "--remote-allow-origins=*",
               f"--user-data-dir={profile}", "--window-size=1280,960"]
    if proxy:
        command.append(f"--proxy-server={proxy}")
    command.append("https://www.krea.ai/video")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        return asyncio.run(_login_cdp(port, email, password, timeout))
    except KreaLoginError:
        raise
    except Exception as exc:
        raise KreaLoginError(f"Krea native browser login failed: {type(exc).__name__}",
                             "network_error") from exc
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
