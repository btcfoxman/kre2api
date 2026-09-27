from types import SimpleNamespace

import pytest

from app.client import KreaClient, KreaError
from app.service import Service
from app.store import Store


def client():
    return KreaClient(cookies=[], proxy_url="socks5://account-proxy:1080")


def response(status=200):
    return SimpleNamespace(status_code=status, content=b"image", headers={"content-type": "image/png"})


def test_media_download_tries_direct_before_account_and_backup_proxy(monkeypatch):
    monkeypatch.setenv("KR_MEDIA_FALLBACK_PROXY_URL", "http://backup-proxy:8080")
    routes = []

    def get(_source, **kwargs):
        routes.append(kwargs["proxy"])
        if len(routes) == 1:
            raise OSError("temporary TLS failure")
        if len(routes) == 2:
            return response(503)
        return response()

    monkeypatch.setattr("app.client.requests.get", get)
    data, filename, mime = client()._download_media("https://media.example/ref.png")
    assert (data, filename, mime) == (b"image", "ref.png", "image/png")
    assert routes == [None, "socks5://account-proxy:1080", "http://backup-proxy:8080"]


def test_media_download_uses_direct_success_without_proxy(monkeypatch):
    routes = []
    monkeypatch.setattr("app.client.requests.get", lambda _source, **kwargs:
                        routes.append(kwargs["proxy"]) or response())
    assert client()._download_media("https://media.example/ref.png")[0] == b"image"
    assert routes == [None]


def test_media_download_exhausts_routes_with_stable_error_code(monkeypatch):
    monkeypatch.delenv("KR_MEDIA_FALLBACK_PROXY_URL", raising=False)
    monkeypatch.setattr("app.client.requests.get", lambda _source, **kwargs: response(403))
    with pytest.raises(KreaError) as error:
        client()._download_media("https://media.example/ref.png")
    assert error.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert "HTTP 403" in str(error.value)


def test_failed_media_download_returns_normalized_task_error(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "media.db"))
    account = store.upsert_account(name="test", cookies=[{"name": "session", "value": "secret"}],
                                   project_id="project")
    normalized = {"model": "sd-2-0", "duration": 5, "resolution": "720p",
                  "images": [], "videos": [], "audios": []}
    assert store.reserve_task(task_id="kre_media", account_id=account["id"],
                              model="sd-2-0", request={}, normalized=normalized,
                              estimated_cost=1, balance=10)
    service = Service(store)
    media_client = client()
    monkeypatch.setattr(media_client, "prepare_media", lambda _normalized:
                        (_ for _ in ()).throw(KreaError("media download failed: SSLError",
                                                     retryable=True, code="MEDIA_DOWNLOAD_FAILED")))
    monkeypatch.setattr(service, "_client", lambda _account: media_client)
    service._process("kre_media")
    assert store.task("kre_media")["error"] == "素材下载失败，请检查素材链接后重试~"
    assert store.account(account["id"])["last_error"] == "media download failed: SSLError"
    service.stop()
