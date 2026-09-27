import uuid

from app.credentials import encrypt_password
from app.service import Service
from app.store import Store


def test_manual_login_preserves_account_identity_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("KR_DATA_DIR", str(tmp_path))
    store = Store(str(tmp_path / "accounts.db"))
    account = store.upsert_account(name="owner@example.com",
                                   password_ciphertext=encrypt_password("secret"),
                                   proxy_url="socks5://proxy:20012")
    opened = []

    class Browser:
        def __init__(self, account_id, proxy_url, data_dir):
            assert account_id == account["id"]
            assert proxy_url == "socks5://proxy:20012"
            opened.append(self)
            self.closed = False

        def snapshot(self):
            return {"image": "data:image/jpeg;base64,AA==", "signed_in": False}

        def action(self, body, email, password):
            assert (body, email, password) == ({"action": "login"}, "owner@example.com", "secret")
            return {"phase": "signed_in"}

        def capture(self, email):
            assert email == "owner@example.com"
            return [{"name": "session", "value": "private"}], "Chrome", ""

        def close(self):
            self.closed = True

    class Client:
        def __init__(self, *, cookies, proxy_url, user_agent):
            assert cookies[0]["value"] == "private"
            assert proxy_url == "socks5://proxy:20012"
            assert user_agent == "Chrome"

        def balance(self):
            return 28.5

    monkeypatch.setattr("app.service.ManualBrowser", Browser)
    monkeypatch.setattr("app.service.KreaClient", Client)
    service = Service(store)
    try:
        assert service.open_manual_browser(account["id"])["signed_in"] is False
        assert service.schedule_login(account["id"]) is False
        assert service.manual_browser_action(account["id"], {"action": "login"}) == {
            "action_result": {"phase": "signed_in"}}
        saved = service.complete_manual_browser(account["id"])
        assert (saved["login_status"], saved["balance"]) == ("ready", 28.5)
        assert uuid.UUID(saved["project_id"]).version == 7
        assert saved["cookies"][0]["value"] == "private"
        assert opened[0].closed
    finally:
        service.stop()
