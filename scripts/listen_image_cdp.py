"""Passively record sanitized Krea image API traffic from an existing Chrome CDP port.

The output is JSONL under kre2api/data/listen/ (gitignored). Authentication,
prompts, source images and generated images are never written to disk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websocket


SAFE_STRING_KEYS = {
    "model", "model_name", "modelname", "aspect_ratio", "aspectratio", "ratio",
    "resolution", "quality", "format", "output_format", "style", "sampler",
    "status", "state", "type", "mode", "provider", "operationname",
    "tasktype", "generationtype", "preset", "size", "visibility", "scope",
}
SECRET_RE = re.compile(
    r"password|secret|cookie|token|authorization|credential|prompt|text|email|"
    r"data|base64|blob|image|file|media|asset|url|uri|signed|key|header|"
    r"captcha|turnstile|session|source|input|output|content|message",
    re.I,
)
ID_RE = re.compile(r"(?:^|_)(?:id|uuid)$|id$", re.I)
ASSET_RE = re.compile(r"\.(?:js|css|png|jpe?g|webp|gif|svg|woff2?|mp4|webm)(?:$|\?)", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_url(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    return {
        "host": parsed.hostname or "",
        "path": parsed.path,
        "query_keys": sorted(urllib.parse.parse_qs(parsed.query).keys()),
    }


def scrub(value, key: str = "", depth: int = 0):
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, dict):
        return {str(k): scrub(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return {"count": len(value), "items": [scrub(v, key, depth + 1) for v in value[:3]]}
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if not isinstance(value, str):
        return f"[{type(value).__name__}]"
    if ID_RE.search(key):
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]
    if SECRET_RE.search(key):
        return f"[redacted string, {len(value)} chars]"
    if key.lower() in SAFE_STRING_KEYS and len(value) <= 120:
        return value
    return f"[string, {len(value)} chars]"


def scrub_body(body: str, is_base64: bool = False):
    if is_base64:
        return f"[base64 response, {len(body)} chars]"
    if len(body) > 2_000_000:
        return f"[body too large, {len(body)} chars]"
    if "Content-Disposition: form-data;" in body:
        fields = {}
        for part in re.split(r"--[-\w]+\r?\n", body):
            match = re.search(r'Content-Disposition: form-data;\s*name="([^"]+)"', part)
            if not match:
                continue
            name = match.group(1)
            content = part.split("\r\n\r\n", 1)
            if len(content) == 1:
                content = part.split("\n\n", 1)
            value = content[1].rstrip("\r\n-") if len(content) == 2 else ""
            if name == "payload":
                fields[name] = scrub_body(value)
            elif name.lower() in {"createasset", "scope", "type"}:
                fields[name] = scrub(value, name)
            else:
                fields[name] = f"[multipart field, {len(value)} chars]"
        return fields or "[multipart body]"
    try:
        return scrub(json.loads(body))
    except (ValueError, TypeError):
        if "=" in body and len(body) < 200_000:
            form = urllib.parse.parse_qs(body, keep_blank_values=True)
            if form:
                return {k: scrub(v, k) for k, v in form.items()}
        return f"[non-JSON body, {len(body)} chars]"


class Listener:
    def __init__(self, port: int, output: Path):
        self.port = port
        self.output = output
        self.next_id = 0
        self.pending = {}
        self.requests = {}
        self.targets = {}
        self.attached = set()
        self.count = 0

    def record(self, event: dict):
        event["time"] = now()
        with self.output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.count += 1
        kind = event.get("kind", "event")
        endpoint = event.get("endpoint") or {}
        print(f"{event['time']} {kind} {endpoint.get('host', '')}{endpoint.get('path', '')}", flush=True)

    def send(self, method: str, params: dict | None = None, session: str | None = None, context=None):
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        if context is not None:
            self.pending[self.next_id] = context
        self.ws.send(json.dumps(message))

    def relevant(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return bool(parsed.hostname and (parsed.hostname == "krea.ai" or parsed.hostname.endswith(".krea.ai"))) and not ASSET_RE.search(parsed.path)

    def run(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5) as response:
            version = json.load(response)
        browser_url = version["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(browser_url, timeout=2, suppress_origin=True)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.record({"kind": "listener_started", "browser": version.get("Browser"), "port": self.port})
        self.send("Target.setDiscoverTargets", {"discover": True})
        self.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        print("READY: CDP network listener attached; waiting for manual image operations.", flush=True)
        while True:
            try:
                message = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except KeyboardInterrupt:
                break
            self.handle(message)

    def handle(self, message: dict):
        if "id" in message:
            self.handle_result(message)
            return
        method = message.get("method", "")
        params = message.get("params") or {}
        session = message.get("sessionId")
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            target_id = info.get("targetId", "")
            session_id = params.get("sessionId")
            url = info.get("url", "")
            self.targets[target_id] = safe_url(url)
            if info.get("type") in {"page", "shared_worker", "service_worker"} and self.relevant(url):
                self.attached.add(session_id)
                self.send("Network.enable", {"maxTotalBufferSize": 20_000_000, "maxResourceBufferSize": 5_000_000}, session_id)
                self.record({"kind": "target_attached", "target_type": info.get("type"), "endpoint": safe_url(url)})
            return
        if method == "Target.targetInfoChanged":
            info = params.get("targetInfo") or {}
            self.targets[info.get("targetId", "")] = safe_url(info.get("url", ""))
            return
        if session not in self.attached:
            return
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            url = request.get("url", "")
            if not self.relevant(url):
                return
            resource_type = params.get("type", "")
            if resource_type not in {"XHR", "Fetch", "WebSocket", "Other"}:
                return
            request_id = params.get("requestId", "")
            self.requests[(session, request_id)] = {"endpoint": safe_url(url), "type": resource_type}
            entry = {"kind": "request", "endpoint": safe_url(url), "method": request.get("method"), "resource_type": resource_type}
            if "postData" in request:
                entry["body"] = scrub_body(request["postData"])
            self.record(entry)
            if request.get("hasPostData") and "postData" not in request:
                self.send("Network.getRequestPostData", {"requestId": request_id}, session, ("post_data", session, request_id))
            return
        if method == "Network.responseReceived":
            request_id = params.get("requestId", "")
            info = self.requests.get((session, request_id))
            if not info:
                return
            response = params.get("response") or {}
            info["mime"] = response.get("mimeType", "")
            self.record({"kind": "response", "endpoint": info["endpoint"], "status": response.get("status"), "mime": info["mime"]})
            return
        if method == "Network.loadingFinished":
            request_id = params.get("requestId", "")
            info = self.requests.pop((session, request_id), None)
            if info and ("json" in info.get("mime", "") or info.get("type") in {"XHR", "Fetch"}):
                self.send("Network.getResponseBody", {"requestId": request_id}, session, ("response_body", info["endpoint"]))
            return
        if method in {"Network.webSocketFrameSent", "Network.webSocketFrameReceived"}:
            frame = params.get("response") or {}
            payload = frame.get("payloadData", "")
            self.record({"kind": "ws_sent" if method.endswith("Sent") else "ws_received", "body": scrub_body(payload, frame.get("opcode") == 2)})

    def handle_result(self, message: dict):
        context = self.pending.pop(message["id"], None)
        if not context or "result" not in message:
            return
        result = message["result"]
        if context[0] == "post_data":
            info = self.requests.get((context[1], context[2]))
            if info:
                self.record({"kind": "request_body", "endpoint": info["endpoint"], "body": scrub_body(result.get("postData", ""))})
        elif context[0] == "response_body":
            self.record({"kind": "response_body", "endpoint": context[1], "body": scrub_body(result.get("body", ""), result.get("base64Encoded", False))})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9236)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "listen" / f"image-{datetime.now():%Y%m%d-%H%M%S}.jsonl")
    args = parser.parse_args()
    Listener(args.port, args.output).run()
