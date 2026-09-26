"""Sync a manually logged-in Krea Chrome profile into KRE2API.

Example:
  KR_SYNC_TOKEN=... python scripts/import_cdp.py --cdp http://127.0.0.1:9236 \
      --name h04-joa --service https://kre2api.aiid.edu.kg \
      --capture ../output/cdp-krea/latest-capture/events.jsonl

Only account metadata is printed. Cookie values are sent to the configured
service over HTTPS and are never written by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import websockets
def _get_json(url: str) -> dict | list:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.load(response)


async def _cdp_session(base: str) -> tuple[list[dict], str, str]:
    targets = _get_json(base.rstrip("/") + "/json/list")
    page = next((item for item in targets if item.get("type") == "page"
                 and urlparse(item.get("url", "")).hostname == "www.krea.ai"), None)
    if not page:
        raise RuntimeError("CDP has no Krea page")
    async with websockets.connect(page["webSocketDebuggerUrl"], origin=None,
                                  proxy=None, open_timeout=5) as socket:
        commands = [
            {"id": 1, "method": "Network.getCookies", "params": {"urls": ["https://www.krea.ai/video"]}},
            {"id": 2, "method": "Runtime.evaluate", "params": {
                "expression": "navigator.userAgent", "returnByValue": True}},
        ]
        for command in commands:
            await socket.send(json.dumps(command))
        answers = {}
        while len(answers) < 2:
            message = json.loads(await socket.recv())
            if message.get("id") in (1, 2):
                answers[message["id"]] = message
        cookies = answers[1].get("result", {}).get("cookies", [])
        agent = answers[2].get("result", {}).get("result", {}).get("value", "")
        if not any(item.get("name") == "sb-superb-auth-token" for item in cookies):
            raise RuntimeError("Krea session cookie is missing; log in through the browser first")
        return cookies, str(agent), page.get("url", "")


def _project_from_capture(path: Path) -> str:
    if not path.is_file():
        return ""
    project = ""
    for line in path.open(encoding="utf-8"):
        try:
            item = json.loads(line)
            if item.get("kind") != "request" or not item.get("request", {}).get("url", "").endswith("/api/jobs/v2/new/videoV2"):
                continue
            post_data = item["request"].get("postData", "")
            match = re.search(r'name="payload"\r?\n\r?\n(\{.*?\})\r?\n--', post_data, re.S)
            if match:
                project = str(json.loads(match.group(1)).get("project") or "")
        except (ValueError, TypeError, KeyError):
            continue
    return project


def _proxy_from_launcher(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'--proxy-server(?:=|\s+)("[^"]+"|\S+)', text)
    return match.group(1).strip('"') if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp", default="http://127.0.0.1:9236")
    parser.add_argument("--name", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--proxy-url", default="")
    parser.add_argument("--project-id", default="")
    args = parser.parse_args()
    service = args.service.rstrip("/")
    parsed = urlparse(service)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        parser.error("remote service URL must use HTTPS")
    token = os.getenv("KR_SYNC_TOKEN") or getpass.getpass("KR_SYNC_TOKEN: ")
    if not token:
        parser.error("KR_SYNC_TOKEN is required")
    cookies, user_agent, browser_url = asyncio.run(_cdp_session(args.cdp))
    project = args.project_id or (_project_from_capture(args.capture) if args.capture else "")
    if not project:
        from urllib.parse import parse_qs
        project = parse_qs(urlparse(browser_url).query).get("project", [""])[0]
    if not project:
        parser.error("project ID not found; provide --capture or --project-id")
    proxy = args.proxy_url or (_proxy_from_launcher(args.launcher) if args.launcher else "")
    payload = {"name": args.name, "cookies": cookies, "user_agent": user_agent,
               "proxy_url": proxy, "project_id": project, "enabled": True}
    try:
        request = urllib.request.Request(
            service + "/api/accounts/sync/browser",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json",
                     "User-Agent": user_agent},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=30) as response:
            account = json.load(response)
        print(f"Synced {account['name']} as account {account['id']} ({len(cookies)} browser cookies)")
        return 0
    except urllib.error.HTTPError as exc:
        try:
            issue = json.load(exc).get("detail")
            if isinstance(issue, list):
                detail = "; ".join(f"{item.get('loc')}: {item.get('msg')}"
                                   for item in issue[:3] if isinstance(item, dict))
            else:
                detail = str(issue)[:300]
        except (ValueError, AttributeError, TypeError):
            detail = "request rejected"
        print(f"Sync failed: HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
