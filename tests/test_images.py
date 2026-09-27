import json

import pytest

from app.catalog import normalize_request, public_models
from app.client import KreaClient
from app.service import Service
from app.store import Store


def test_seedream_image_payload_matches_captured_quote_and_submit(monkeypatch):
    request = normalize_request({
        "model": "seedream-5.0-pro", "prompt": "A quiet landscape",
        "width": 1536, "height": 2720, "n": 2,
        "image_urls": [{"url": "https://app-uploads.krea.ai/reference.png",
                        "strength": 0.4}],
    })
    assert request["kind"] == "image"
    assert request["provider"] == "seedream5pro"
    assert request["width"] == 1536 and request["height"] == 2720
    assert request["batch_size"] == 2
    quote = KreaClient.quote_input(request, project_id="image-project")
    assert quote["scope"] == "image" and quote["batchSize"] == 2
    assert quote["inputParams"]["styleImages"] == [{
        "url": "https://app-uploads.krea.ai/reference.png",
        "strength": 0.4, "source": "upload",
    }]
    assert "prompt" not in quote["inputParams"]
    payload = KreaClient.image_generation_input(request, "image-project")
    assert payload["prompt"] == "A quiet landscape"
    assert payload["project"] == "image-project"
    assert payload["batchSize"] == 2
    client = KreaClient(cookies=[])
    paths = []

    def request_stub(method, path, **_kwargs):
        paths.append((method, path))
        return [{"job_id": "image-a"}, {"job_id": "image-b"}]

    monkeypatch.setattr(client, "_request", request_stub)
    assert len(client.submit(request, "image-project")) == 2
    assert paths == [("POST", "/api/jobs/v2/new/externalImage")]
    assert any(model["id"] == "seedream-5.0-pro" and model["kind"] == "image"
               for model in public_models())


def test_seedream_image_rejects_invalid_media_and_dimensions():
    converted = normalize_request({
        "model": "seedream-5.0-pro", "prompt": "img1 beside Image2",
        "image_urls": ["https://example.test/one.png", "https://example.test/two.png"],
    })
    assert converted["prompt"] == "@Image1 beside @Image2"
    with pytest.raises(ValueError, match="multiples of 16"):
        normalize_request({"model": "seedream-5.0-pro", "prompt": "test",
                           "width": 1025, "height": 1024})
    with pytest.raises(ValueError, match="image references only"):
        normalize_request({"model": "seedream-5.0-pro", "prompt": "test",
                           "video_urls": ["https://example.test/a.mp4"]})
    with pytest.raises(ValueError, match="Image reference 2"):
        normalize_request({"model": "seedream-5.0-pro", "prompt": "use img2",
                           "image_urls": ["https://example.test/one.png"]})


def test_seedream_batch_polls_every_job_and_returns_all_images(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "images.db"))
    account = store.upsert_account(name="image-account",
                                   cookies=[{"name": "session", "value": "test"}],
                                   project_id="image-project")
    normalized = normalize_request({"model": "seedream-5.0-pro", "prompt": "test", "n": 2})
    assert store.reserve_task(task_id="kre_image", account_id=account["id"],
                              model=normalized["model"], request={"prompt": "test"},
                              normalized=normalized, estimated_cost=100, balance=500)
    store.update_task("kre_image", status="running", upstream_job_id="image-a",
                      upstream_response_json=json.dumps({"submission": [
                          {"job_id": "image-a"}, {"job_id": "image-b"}]}))
    service = Service(store)
    calls = []

    class Client:
        def job(self, job_id, *, image=False):
            assert image is True
            calls.append(job_id)
            return {"job_id": job_id, "status": "completed",
                    "result": {"image_urls": [f"https://app-uploads.krea.ai/{job_id}.png"]}}

        def balance(self):
            return 400

        def export_cookies(self):
            return account["cookies"]

    monkeypatch.setattr(service, "_client", lambda _account: Client())
    try:
        service._poll_one(store.task("kre_image"))
        task = store.task("kre_image")
        assert calls == ["image-a", "image-b"]
        assert task["status"] == "succeeded"
        assert task["result_urls"] == [
            "https://app-uploads.krea.ai/image-a.png",
            "https://app-uploads.krea.ai/image-b.png",
        ]
        assert service.public_task(task)["object"] == "image"
        assert task["actual_cost"] == 100
    finally:
        service.stop()


def test_seedream_service_reserves_quotes_and_submits_batch(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "submit.db"))
    account = store.upsert_account(name="image-account",
                                   cookies=[{"name": "session", "value": "test"}],
                                   project_id="image-project")
    service = Service(store)
    submissions = []

    class Client:
        def balance(self):
            return 500

        def estimate(self, normalized, *, video_seconds=0, project_id=""):
            assert normalized["kind"] == "image"
            assert project_id == "image-project"
            return 75

        def prepare_media(self, normalized):
            return normalized, 0.0

        def image_generation_input(self, normalized, project_id):
            return KreaClient.image_generation_input(normalized, project_id)

        def submit(self, normalized, project_id):
            submissions.append((normalized["batch_size"], project_id))
            return [{"job_id": "image-a"}, {"job_id": "image-b"}]

        def export_cookies(self):
            return account["cookies"]

    monkeypatch.setattr(service, "_client", lambda _account: Client())
    monkeypatch.setattr(service, "_enqueue", lambda _task_id: None)
    try:
        created = service.create({"model": "seedream-5.0-pro", "prompt": "test",
                                  "n": 2, "resolution": "1.5k", "aspect_ratio": "1:1"})
        service._process(created["id"])
        task = store.task(created["id"])
        assert submissions == [(2, "image-project")]
        assert task["status"] == "running"
        assert task["upstream_job_id"] == "image-a"
        assert task["upstream_request"]["path"] == "/api/jobs/v2/new/externalImage"
        assert (task["normalized"]["width"], task["normalized"]["height"]) == (1536, 1536)
        assert len(task["upstream_response"]["submission"]) == 2
    finally:
        service.stop()
