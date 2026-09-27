"""Seedream image request normalization from the captured Krea image workflow."""

from __future__ import annotations

import re
from typing import Any


IMAGE_MODEL = "seedream-5.0-pro"
IMAGE_ALIASES = {IMAGE_MODEL, "seedream5pro", "doubao-seedream-5-0-pro-260628"}
RATIOS = ("16:9", "9:16", "1:1", "2:3", "3:2")
PRESET_DIMENSIONS = {
    "1.5k": {"1:1": (1536, 1536), "16:9": (1536, 864),
             "9:16": (864, 1536), "2:3": (1024, 1536), "3:2": (1536, 1024)},
    "2k": {"1:1": (2048, 2048), "16:9": (2048, 1152),
           "9:16": (1152, 2048), "2:3": (1248, 1888), "3:2": (1888, 1248)},
    "3k": {"1:1": (2720, 2720), "16:9": (2720, 1536),
           "9:16": (1536, 2720), "2:3": (1808, 2720), "3:2": (2720, 1808)},
}
IMAGE_REFERENCE = re.compile(
    r"(?<![\w@])(?:图片|图|image|img)\s*[-_ ]?\s*(?P<index>\d+)(?!\w)", re.I
)


def is_image_model(name: Any) -> bool:
    return str(name or "").strip().lower() in IMAGE_ALIASES


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    number = _number(value, name, minimum, maximum)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _first(data: dict[str, Any], *names: str, default: Any) -> Any:
    return next((data[name] for name in names if data.get(name) is not None
                 and data[name] != ""), default)


def _dimensions(data: dict[str, Any]) -> tuple[int, int, str]:
    size = str(data.get("size") or "").strip().lower().replace("×", "x")
    width, height = data.get("width"), data.get("height")
    if "x" in size:
        left, _, right = size.partition("x")
        width, height = left, right
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("width and height must be provided together")
        width = _integer(width, "width", 512, 4096)
        height = _integer(height, "height", 512, 4096)
        if width % 16 or height % 16:
            raise ValueError("image width and height must be multiples of 16")
        if width * height > 9_000_000:
            raise ValueError("image dimensions exceed 9 megapixels")
        return width, height, f"{width}x{height}"
    ratio = str(data.get("aspect_ratio") or data.get("aspectRatio") or data.get("ratio") or "16:9").replace("/", ":")
    if ratio not in RATIOS:
        raise ValueError(f"unsupported image aspect_ratio: {ratio}")
    resolution = str(data.get("resolution") or "2k").strip().lower()
    if resolution not in PRESET_DIMENSIONS:
        raise ValueError("image resolution must be 1.5k, 2k or 3k, or provide exact width and height")
    return *PRESET_DIMENSIONS[resolution][ratio], resolution


def normalize_image_request(data: dict[str, Any]) -> dict[str, Any]:
    if not is_image_model(data.get("model")):
        raise ValueError(f"unsupported image model: {data.get('model')}")
    prompt = str(data.get("prompt") or data.get("input") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    width, height, resolution = _dimensions(data)
    quantity = _integer(_first(data, "n", "quantity", "batch_size", default=1),
                        "n", 1, 4)
    if data.get("video_url") or data.get("video_urls") or data.get("audio_url") or data.get("audio_urls"):
        raise ValueError("Seedream 5 Pro accepts image references only")

    values = []
    for key in ("image_urls", "reference_images", "images"):
        selected = data.get(key) or []
        values.extend(selected if isinstance(selected, list) else [selected])
    if data.get("image_url"):
        values = [data["image_url"], *values]
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        item = {"url": item} if isinstance(item, str) else item
        if not isinstance(item, dict):
            raise ValueError("image references must be URLs or objects")
        url = str(item.get("url") or item.get("value") or "").strip()
        if not url.startswith(("https://", "http://", "data:image/")):
            raise ValueError("image references must be HTTP(S) URLs or image data URLs")
        if url in seen:
            continue
        seen.add(url)
        images.append({"url": url, "strength": _number(item.get("strength", data.get("reference_strength", 0.4)),
                                                       "reference strength", 0, 1),
                       "source": "upload"})
    if len(images) > 10:
        raise ValueError("Seedream 5 Pro accepts at most 10 image references")
    def replace_reference(match: re.Match[str]) -> str:
        index = int(match.group("index"))
        if index == 0:
            return match.group(0)
        if index > len(images):
            raise ValueError(f"Image reference {index} has no matching uploaded media")
        return f"@Image{index}"

    prompt = IMAGE_REFERENCE.sub(replace_reference, prompt)
    preset_styles = data.get("preset_styles") or []
    if not isinstance(preset_styles, list):
        raise ValueError("preset_styles must be a list")
    return {
        "kind": "image", "model": IMAGE_MODEL, "provider": "seedream5pro",
        "prompt": prompt, "width": width, "height": height,
        "resolution": resolution, "aspect_ratio": f"{width}:{height}",
        "duration": 0, "batch_size": quantity,
        "strength": _number(data.get("strength", 1), "strength", 0, 1),
        "steps": _integer(data.get("steps", 8), "steps", 1, 100),
        "guidance_scale_flux": _number(data.get("guidance_scale_flux", 3.5),
                                       "guidance_scale_flux", 0, 30),
        "preset_styles": preset_styles, "images": images, "videos": [], "audios": [],
    }


def public_image_models() -> list[dict[str, Any]]:
    return [{
        "id": IMAGE_MODEL, "object": "model", "owned_by": "krea",
        "kind": "image", "upstream_provider": "seedream5pro",
        "capabilities": {"resolutions": ["1.5k", "2k", "3k"],
                         "aspect_ratios": list(RATIOS), "max_images": 10,
                         "batch_sizes": [1, 2, 3, 4],
                         "parameters": ["width", "height", "steps", "guidance_scale_flux",
                                        "strength", "reference_strength", "preset_styles"]},
    }]
