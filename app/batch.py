"""Parse AK-style email|password|proxy account imports."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def normalize_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"(?:\[[0-9a-fA-F:]+\]|[^\s:/]+):\d{1,5}", value):
        value = "http://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ValueError("代理须为 host:port 或 http(s)/socks5 URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not port or not 1 <= port <= 65535 or parsed.path not in {"", "/"}:
        raise ValueError("代理地址或端口无效")
    return value


def parse_batch(text: str) -> tuple[list[dict], list[dict]]:
    entries: list[dict] = []
    errors: list[dict] = []
    indexes: dict[str, int] = {}
    if len(text) > 2_000_000:
        raise ValueError("批量导入文本过大")
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(entries) >= 1000:
            raise ValueError("每次最多导入 1000 个账号")
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in {2, 3}:
            errors.append({"line": line_number, "message": "格式须为 邮箱|密码|代理，代理可留空"})
            continue
        email, password = parts[:2]
        if not re.fullmatch(r"[^\s@|]+@[^\s@|]+\.[^\s@|]+", email):
            errors.append({"line": line_number, "message": "邮箱格式无效"})
            continue
        if not password:
            errors.append({"line": line_number, "message": "密码不能为空"})
            continue
        try:
            proxy = normalize_proxy(parts[2] if len(parts) == 3 else "")
        except ValueError as exc:
            errors.append({"line": line_number, "message": str(exc)})
            continue
        item = {"line": line_number, "name": email, "password": password, "proxy_url": proxy}
        key = email.casefold()
        if key in indexes:
            entries[indexes[key]] = item
        else:
            indexes[key] = len(entries)
            entries.append(item)
    return entries, errors
