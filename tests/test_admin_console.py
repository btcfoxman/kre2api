import importlib

from fastapi.testclient import TestClient

from app.service import Service
from app.store import Store


def test_admin_settings_account_and_task_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_API_KEY", "test-api-key")
    monkeypatch.setenv("KR_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("KR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KR_DATABASE_PATH", str(tmp_path / "console.db"))
    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        assert client.get("/api/admin/settings").status_code == 401
        assert client.post("/api/admin/login", json={"token": "test-admin-token"}).status_code == 200
        settings = client.get("/api/admin/settings").json()
        assert settings["poll_interval_seconds"] >= 2
        assert client.patch("/api/admin/settings", json={"task_workers": 99}).status_code == 422
        assert client.patch("/api/admin/settings", json={"poll_interval_seconds": 7, "task_timeout_seconds": 240}).json()["task_timeout_seconds"] == 240
        restored = Service(Store(str(tmp_path / "console.db")))
        assert (restored.poll_seconds, restored.timeout_seconds) == (7, 240)
        restored.stop()

        account = client.post("/api/admin/accounts", json={
            "name": "browser", "cookies": [{"name": "session", "value": "private"}],
            "project_id": "project-one", "user_agent": "Chrome",
        }).json()
        assert "cookies" not in account
        updated = client.post("/api/admin/accounts", json={
            "name": "browser", "project_id": "project-two", "proxy_url": "socks5://proxy:1000",
        }).json()
        assert updated["id"] == account["id"]
        assert updated["user_agent"] == "Chrome"
        assert main.store.account(account["id"])["cookies"][0]["value"] == "private"
        normalized = {"model": "sd-2-0", "provider": "seedance-2", "prompt": "test",
                      "duration": 5, "resolution": "720p", "aspect_ratio": "16:9",
                      "images": [], "videos": [], "audios": []}
        assert main.store.reserve_task(task_id="kre_detail", account_id=account["id"],
                                       model="sd-2-0", request={"prompt": "test"},
                                       normalized=normalized, estimated_cost=10, balance=100)
        detail = client.get("/api/admin/tasks/kre_detail").json()
        assert detail["request"]["prompt"] == "test"
        assert detail["normalized"]["provider"] == "seedance-2"
        assert detail["caller_response"]["id"] == "kre_detail"
        main.store.update_task("kre_detail", status="failed", reserved_cost=0, error="upstream error")
        assert client.delete("/api/admin/tasks/finished").json() == {"deleted": 1}
        assert client.get("/api/admin/tasks/kre_detail").status_code == 404
