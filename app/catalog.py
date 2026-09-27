"""Public model aliases and Krea Video request normalization.

The provider names and limits are based on the Krea Video frontend loaded on
2026-09-27. The upstream estimate endpoint remains the source of truth for
compute units.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Model:
    name: str
    provider: str
    resolution: str | None
    durations: range
    resolutions: tuple[str, ...]
    ratios: tuple[str, ...]
    images: int
    videos: int
    audios: int
    total_references: int
    max_reference_seconds: int | None = None


RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16", "21:9")
SEEDANCE_2 = (range(4, 16), RATIOS, 9, 3, 3, 12, 15)
SEEDANCE_25 = (range(4, 31), RATIOS, 30, 10, 10, 50, 30)
H3 = (range(5, 16), RATIOS, 9, 3, 3, 15, 15)
WAN = (range(2, 31), RATIOS[:-1], 10, 5, 5, 20, None)


def _model(name: str, provider: str, resolution: str | None,
           resolutions: tuple[str, ...], limits: tuple[Any, ...]) -> Model:
    durations, ratios, images, videos, audios, total, seconds = limits
    return Model(name, provider, resolution, durations, resolutions, ratios,
                 images, videos, audios, total, seconds)


MODELS: dict[str, Model] = {
    "sd-2-0": _model("sd-2-0", "seedance-2", "720p", ("480p", "720p", "1080p", "4k"), SEEDANCE_2),
    "sd-2-0-1080p": _model("sd-2-0-1080p", "seedance-2", "1080p", ("480p", "720p", "1080p", "4k"), SEEDANCE_2),
    "sd-2-0-4k": _model("sd-2-0-4k", "seedance-2", "4k", ("480p", "720p", "1080p", "4k"), SEEDANCE_2),
    "sd-2-0-fast": _model("sd-2-0-fast", "seedance-2-fast", "720p", ("480p", "720p"), SEEDANCE_2),
    "sd-2-0-mini": _model("sd-2-0-mini", "seedance-2-mini", "720p", ("480p", "720p"), SEEDANCE_2),
    "sd-2-0-fast-480p": _model("sd-2-0-fast-480p", "seedance-2-fast", "480p", ("480p", "720p"), SEEDANCE_2),
    "sd-2-5": _model("sd-2-5", "seedance-2-5", "720p", ("480p", "720p", "1080p"), SEEDANCE_25),
    "sd-2-5-480p": _model("sd-2-5-480p", "seedance-2-5", "480p", ("480p", "720p", "1080p"), SEEDANCE_25),
    "sd-2-5-1080p": _model("sd-2-5-1080p", "seedance-2-5", "1080p", ("480p", "720p", "1080p"), SEEDANCE_25),
    # Krea's MiniMax H3 is fixed at 2K. Its H3 Max variant offers 768p.
    "minimax-h3-768p": _model("minimax-h3-768p", "minimax_h3max", "768p", ("480p", "768p", "1080p"), (range(5, 16), RATIOS, 12, 7, 7, 12, 15)),
    "minimax-h3": _model("minimax-h3", "minimax_hailuo3", None, ("2k",), H3),
    "wan-3.0": _model("wan-3.0", "wan30", "720p", ("480p", "720p", "1080p"), WAN),
    "wan-3.0-480p": _model("wan-3.0-480p", "wan30", "480p", ("480p", "720p", "1080p"), WAN),
    "wan-3.0-1080p": _model("wan-3.0-1080p", "wan30", "1080p", ("480p", "720p", "1080p"), WAN),
}


def _media_items(data: dict[str, Any], singular: str, plural: str) -> list[dict[str, Any]]:
    values = data.get(plural) or []
    if not isinstance(values, list):
        values = [values]
    if data.get(singular):
        values = [data[singular], *values]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise ValueError(f"{plural} entries must be URLs or objects")
        url = str(item.get("url") or item.get("value") or item.get("media_url") or "").strip()
        if not url or url in seen:
            continue
        if not url.startswith(("https://", "http://", "data:")):
            raise ValueError(f"{plural} entries must be absolute URLs or data URLs")
        seen.add(url)
        result.append({"url": url, "duration": item.get("duration"), "name": item.get("name")})
    return result


_REFERENCE = re.compile(
    r"(?<![\w@])(?P<kind>视频|视|video|v|图片|图|image|img|音频|音|audio)\s*[-_ ]?\s*(?P<index>\d+)(?!\w)",
    re.IGNORECASE,
)
_ALIASES = {
    "视频": "Video", "视": "Video", "video": "Video", "v": "Video",
    "图片": "Image", "图": "Image", "image": "Image", "img": "Image",
    "音频": "Audio", "音": "Audio", "audio": "Audio",
}


def normalize_prompt(prompt: str, counts: dict[str, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        kind = _ALIASES[match.group("kind").lower()]
        index = int(match.group("index"))
        if index == 0:
            # Reference labels are one-based. A literal such as "video0" is
            # ordinary prompt text, even when no video was supplied.
            return match.group(0)
        if 1 <= index <= counts[kind]:
            return f"@{kind}{index}"
        raise ValueError(f"{kind} reference {index} has no matching uploaded media")

    result = _REFERENCE.sub(replace, prompt.strip())
    for kind, count in counts.items():
        for index in range(1, count + 1):
            tag = f"@{kind}{index}"
            if not re.search(rf"{re.escape(tag)}(?![\w-])", result, re.IGNORECASE):
                result = f"{result} {tag}".strip()
    return result


def normalize_request(data: dict[str, Any]) -> dict[str, Any]:
    requested = str(data.get("model") or "sd-2-0").strip().lower()
    if requested not in MODELS:
        raise ValueError(f"unsupported model: {requested}")
    model = MODELS[requested]
    duration = int(data.get("duration") or 5)
    if duration not in model.durations:
        raise ValueError(f"{requested} duration must be {model.durations.start}-{model.durations.stop - 1}s")
    resolution = str(data.get("resolution") or model.resolution or "2k").lower()
    if resolution not in model.resolutions:
        raise ValueError(f"{requested} resolution must be one of {', '.join(model.resolutions)}")
    ratio = str(data.get("aspect_ratio") or data.get("aspectRatio") or "16:9")
    if ratio not in model.ratios:
        raise ValueError(f"{requested} aspect_ratio must be one of {', '.join(model.ratios)}")
    prompt = str(data.get("prompt") or data.get("input") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    images = _media_items(data, "image_url", "image_urls")
    videos = _media_items(data, "video_url", "video_urls")
    audios = _media_items(data, "audio_url", "audio_urls")
    for items, maximum, name in ((images, model.images, "images"), (videos, model.videos, "videos"), (audios, model.audios, "audios")):
        if len(items) > maximum:
            raise ValueError(f"{requested} accepts at most {maximum} {name}")
    if len(images) + len(videos) + len(audios) > model.total_references:
        raise ValueError(f"{requested} accepts at most {model.total_references} total references")
    if model.max_reference_seconds:
        for item in videos:
            if item["duration"] is not None and float(item["duration"]) > model.max_reference_seconds:
                raise ValueError(f"{requested} video references must be at most {model.max_reference_seconds}s")
    counts = {"Image": len(images), "Video": len(videos), "Audio": len(audios)}
    result = {
        "model": requested,
        "provider": model.provider,
        "prompt": normalize_prompt(prompt, counts),
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": ratio,
        "generate_audio": bool(data.get("generate_audio", True)),
        "images": images,
        "videos": videos,
        "audios": audios,
    }
    for target, aliases in {
        "start_image": ("start_image", "first_frame", "image"),
        "end_image": ("end_image", "last_frame", "tail_image"),
        "start_video": ("start_video",),
    }.items():
        for alias in aliases:
            if data.get(alias):
                result[target] = data[alias]
                break
    return result


def public_models() -> list[dict[str, Any]]:
    return [{
        "id": spec.name, "object": "model", "owned_by": "krea",
        "capabilities": {"durations": list(spec.durations), "resolutions": list(spec.resolutions),
                         "aspect_ratios": list(spec.ratios), "media_limits": {
                             "images": spec.images, "videos": spec.videos, "audio": spec.audios}},
        "upstream_provider": spec.provider,
    } for spec in MODELS.values()]
