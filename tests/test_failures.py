import json

from app.service import failure_message


def test_generated_audio_moderation_is_normalized_without_leaking_input():
    job = {
        "status": "failed",
        "result": {
            "moderated": True,
            "data": {
                "type": "content_policy_violation",
                "error": "Unexpected status code: 422",
                "msg": json.dumps({"detail": [{
                    "type": "content_policy_violation",
                    "loc": ["body", "generated_video"],
                    "msg": "Output audio has sensitive content. Potential copyright violation.",
                    "input": {"prompt": "private user prompt"},
                }]}),
            },
        },
    }
    refunded = failure_message(job, refund_confirmed=True)
    assert refunded == "生成的视频内容违规，请修改描述后重试，积分已返还~"
    assert "private user prompt" not in refunded
    assert failure_message(job) == "生成的视频内容违规，请修改描述后重试"


def test_nested_upstream_error_is_preserved_when_it_is_not_moderation():
    assert failure_message({"status": "failed", "result": {
        "data": {"error": "Unexpected status code: 422"}
    }}) == "Unexpected status code: 422"
