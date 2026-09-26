"""Krea web protocol client, based on a captured manual Video workflow."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import CurlMime, requests


BASE = "https://www.krea.ai"
ALLOWED_MEDIA_HOSTS = {"app-uploads.krea.ai", "gen.krea.ai", "generations.krea.ai"}
MAX_MEDIA_BYTES = 100 * 1024 * 1024
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass
class KreaError(Exception):
    message: str
    status_code: int = 502
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def _message(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            value = body.get("message") or body.get("error") or body.get("detail")
            if isinstance(value, dict):
                value = value.get("message") or value.get("description")
            if value:
                return str(value)[:600]
    except (ValueError, TypeError):
        pass
    return f"Krea returned HTTP {response.status_code}"


class KreaClient:
    def __init__(self, *, cookies: list[dict[str, Any]], proxy_url: str = "",
                 user_agent: str = "", timeout: int = 60) -> None:
        self.proxy_url = proxy_url.strip()
        self.timeout = timeout
        self.session = requests.Session(impersonate="chrome")
        self.headers = {
            "Referer": f"{BASE}/video",
            "Origin": BASE,
        }
        if user_agent:
            self.headers["User-Agent"] = user_agent
        for item in cookies:
            name, value = str(item.get("name") or ""), str(item.get("value") or "")
            if not name or not value:
                continue
            domain = str(item.get("domain") or "www.krea.ai")
            if not domain.endswith("krea.ai"):
                continue
            self.session.cookies.set(name, value, domain=domain,
                                     path=str(item.get("path") or "/"))

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not path.startswith("/"):
            raise ValueError("Krea path must be absolute")
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("headers", self.headers)
        if self.proxy_url:
            kwargs.setdefault("proxies", {"https": self.proxy_url})
        try:
            response = self.session.request(method, BASE + path, **kwargs)
        except Exception as exc:
            raise KreaError(f"Krea transport: {type(exc).__name__}: {exc}", retryable=True) from exc
        if response.status_code >= 400:
            raise KreaError(_message(response), response.status_code,
                            response.status_code in {408, 409, 425, 429, 500, 502, 503, 504})
        try:
            return response.json()
        except ValueError as exc:
            raise KreaError("Krea returned a non-JSON response", response.status_code) from exc

    def export_cookies(self) -> list[dict[str, str]]:
        return [{"name": cookie.name, "value": cookie.value,
                 "domain": cookie.domain, "path": cookie.path}
                for cookie in self.session.cookies.jar if cookie.domain.endswith("krea.ai")]

    def billing(self) -> dict[str, Any]:
        result = self._request("GET", "/api/billing-data")
        if not isinstance(result, dict) or "balance" not in result:
            raise KreaError("Krea billing response is missing balance")
        return result

    def balance(self) -> float:
        return float(self.billing().get("balance", {}).get("total") or 0)

    @staticmethod
    def quote_input(normalized: dict[str, Any], *, video_seconds: float = 0) -> dict[str, Any]:
        params: dict[str, Any] = {
            "provider": normalized["provider"],
            "duration": normalized["duration"],
            "generateAudio": normalized["generate_audio"],
            "width": 1280,
            "height": 720,
            "aspectRatio": normalized["aspect_ratio"],
            "hasVideoReference": bool(normalized["videos"] or normalized.get("start_video")),
        }
        if normalized["provider"] != "minimax_hailuo3":
            params["resolution"] = normalized["resolution"]
        if normalized["provider"] in {"minimax_hailuo3", "minimax_h3max"}:
            if video_seconds:
                params["referenceVideoSeconds"] = min(15, int(video_seconds + 0.999))
            if normalized["images"]:
                params["referenceImageCount"] = len(normalized["images"])
        return {"scope": "video", "inputParams": params, "promoOptOut": False}

    def estimate(self, normalized: dict[str, Any], *, video_seconds: float = 0) -> float:
        result = self._request("POST", "/api/jobs/estimate",
                               json=self.quote_input(normalized, video_seconds=video_seconds))
        if not isinstance(result, dict) or result.get("status") != "ready":
            raise KreaError("Krea could not estimate compute units")
        cost = float(result.get("computeUnits") or 0)
        if cost <= 0:
            raise KreaError("Krea returned an invalid compute estimate")
        return cost

    def _download_media(self, source: str) -> tuple[bytes, str, str]:
        if source.startswith("data:"):
            match = re.match(r"^data:([^;,]+);base64,(.+)$", source, re.S)
            if not match:
                raise ValueError("media data URL must be base64 encoded")
            data = base64.b64decode(match.group(2), validate=True)
            mime = match.group(1)
            filename = "reference" + (mimetypes.guess_extension(mime) or ".bin")
        elif source.startswith(("https://", "http://")):
            try:
                kwargs: dict[str, Any] = {"timeout": self.timeout, "impersonate": "chrome"}
                if self.proxy_url:
                    kwargs["proxies"] = {"https": self.proxy_url, "http": self.proxy_url}
                response = requests.get(source, **kwargs)
            except Exception as exc:
                raise KreaError(f"media download failed: {type(exc).__name__}", retryable=True) from exc
            if response.status_code >= 400:
                raise KreaError(f"media download returned HTTP {response.status_code}")
            if len(response.content) > MAX_MEDIA_BYTES:
                raise ValueError("media file exceeds 100 MiB")
            data = response.content
            mime = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
            filename = Path(urlparse(source).path).name or "reference"
            filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", filename)[:100]
            if "." not in filename:
                filename += mimetypes.guess_extension(mime) or ".bin"
        else:
            raise ValueError("media URL must use HTTPS, HTTP or data:")
        if not data or len(data) > MAX_MEDIA_BYTES:
            raise ValueError("media file is empty or exceeds 100 MiB")
        return data, filename, mime

    def upload(self, source: str) -> dict[str, Any]:
        if urlparse(source).hostname in ALLOWED_MEDIA_HOSTS:
            return {"imageUrl": source}
        data, filename, mime = self._download_media(source)
        form = CurlMime()
        try:
            form.addpart(name="file", filename=filename, data=data, content_type=mime)
            form.addpart(name="createAsset", data="true")
            result = self._request("POST", "/api/upload", multipart=form)
        finally:
            form.close()
        if not isinstance(result, dict) or not result.get("imageUrl"):
            raise KreaError("Krea upload response is missing imageUrl")
        return result

    def prepare_media(self, normalized: dict[str, Any]) -> tuple[dict[str, Any], float]:
        result = dict(normalized)
        video_seconds = 0.0
        for key in ("images", "videos", "audios"):
            prepared = []
            for item in normalized[key]:
                uploaded = self.upload(item["url"])
                duration = uploaded.get("duration") or item.get("duration")
                if key == "videos" and duration:
                    video_seconds += float(duration)
                prepared.append({**item, "url": uploaded["imageUrl"], "duration": duration})
            result[key] = prepared
        for key in ("start_image", "end_image", "start_video"):
            if result.get(key):
                uploaded = self.upload(str(result[key]))
                result[key] = uploaded["imageUrl"]
        return result, video_seconds

    @staticmethod
    def generation_input(normalized: dict[str, Any], project_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": normalized["provider"],
            "prompt": normalized["prompt"],
            "duration": normalized["duration"],
            "generateAudio": normalized["generate_audio"],
            "width": 1280,
            "height": 720,
            "aspectRatio": normalized["aspect_ratio"],
            "project": project_id,
            "referenceImages": [item["url"] for item in normalized["images"]],
            "referenceVideos": [item["url"] for item in normalized["videos"]],
            "referenceAudios": [item["url"] for item in normalized["audios"]],
        }
        if normalized["provider"] != "minimax_hailuo3":
            result["resolution"] = normalized["resolution"]
        for source, target in (("start_image", "startImage"), ("end_image", "endImage"),
                               ("start_video", "startVideo")):
            if normalized.get(source):
                result[target] = normalized[source]
        return result

    def submit(self, normalized: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
        payload = self.generation_input(normalized, project_id)
        form = CurlMime()
        try:
            form.addpart(name="payload", data=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")))
            result = self._request("POST", "/api/jobs/v2/new/videoV2",
                                   multipart=form)
        finally:
            form.close()
        if not isinstance(result, list) or not result or not result[0].get("job_id"):
            raise KreaError("Krea submission response is missing job_id")
        return result

    def job(self, job_id: str) -> dict[str, Any]:
        result = self._request("GET", "/api/job-status", params={"id": job_id})
        if isinstance(result, list):
            for entry in result:
                if isinstance(entry, dict) and entry.get("job_id") == job_id:
                    return entry
        elif isinstance(result, dict) and result.get("job_id") == job_id:
            return result
        raise KreaError("Krea job status response is missing the requested job")
