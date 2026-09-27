import pytest

from app.catalog import MODELS, normalize_request
from app.client import KreaClient


EXPECTED_ALIASES = {
    "sd-2-0", "sd-2-0-1080p", "sd-2-0-4k", "sd-2-0-fast",
    "sd-2-0-mini", "sd-2-0-fast-480p", "sd-2-5", "sd-2-5-480p",
    "sd-2-5-1080p", "minimax-h3-768p", "minimax-h3", "wan-3.0",
    "wan-3.0-480p", "wan-3.0-1080p",
}


def test_all_requested_aliases_have_upstream_mapping():
    assert set(MODELS) == EXPECTED_ALIASES
    assert MODELS["minimax-h3"].provider == "minimax_hailuo3"
    assert MODELS["minimax-h3-768p"].provider == "minimax_h3max"


def test_prompt_references_match_krea_tagging():
    request = normalize_request({
        "model": "sd-2-5",
        "prompt": "视频1、video2、v3、视4；图片1、图1、img2、Image3；Audio1、音频2",
        "image_urls": [f"https://example.test/{n}.png" for n in range(1, 4)],
        "video_urls": [f"https://example.test/{n}.mp4" for n in range(1, 5)],
        "audio_urls": [f"https://example.test/{n}.mp3" for n in range(1, 3)],
    })
    prompt = request["prompt"]
    for tag in ("@Video1", "@Video2", "@Video3", "@Video4", "@Image1",
                "@Image2", "@Image3", "@Audio1", "@Audio2"):
        assert tag in prompt
    assert prompt.count("@Image1") == 2  # 图1 and 图片1 refer to the same asset.


def test_media_not_mentioned_is_appended_once():
    request = normalize_request({"model": "sd-2-5", "prompt": "镜头缓慢推进 图1",
                                 "image_urls": ["https://example.test/a.png",
                                                "https://example.test/b.png"]})
    assert request["prompt"].count("@Image1") == 1
    assert request["prompt"].count("@Image2") == 1


def test_krea_multipart_payload_shape_matches_manual_capture():
    request = normalize_request({
        "model": "sd-2-0-mini", "prompt": "图1 视频1 音频1", "duration": 5,
        "resolution": "480p", "aspect_ratio": "21:9", "generate_audio": False,
        "image_urls": ["https://app-uploads.krea.ai/a.png"],
        "video_urls": [{"url": "https://app-uploads.krea.ai/b.mp4", "duration": 5}],
        "audio_urls": ["https://app-uploads.krea.ai/c.mp3"],
    })
    payload = KreaClient.generation_input(request, "project-123")
    assert payload["provider"] == "seedance-2-mini"
    assert payload["duration"] == 5
    assert payload["resolution"] == "480p"
    assert payload["aspectRatio"] == "21:9"
    assert payload["project"] == "project-123"
    assert len(payload["referenceImages"]) == len(payload["referenceVideos"]) == len(payload["referenceAudios"]) == 1
    quote = KreaClient.quote_input(request, video_seconds=5)
    assert quote["scope"] == "video"
    assert quote["inputParams"]["hasVideoReference"] is True


def test_unsupported_model_setting_is_rejected():
    with pytest.raises(ValueError, match="resolution"):
        normalize_request({"model": "sd-2-0-fast", "prompt": "scene", "resolution": "4k"})


def test_unbound_media_reference_is_rejected_before_upstream_submission():
    with pytest.raises(ValueError, match="Video reference 2"):
        normalize_request({"model": "sd-2-0-mini", "prompt": "video2 moves left",
                           "video_urls": ["https://example.test/one.mp4"]})


def test_zero_reference_number_is_plain_prompt_text():
    request = normalize_request({"model": "sd-2-0-fast", "prompt": "video0 starts the scene"})
    assert request["prompt"] == "video0 starts the scene"
    assert request["videos"] == []
