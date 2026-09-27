from app.batch import parse_batch
from app.credentials import decrypt_password, encrypt_password
from app.service import Service
from app.store import Store


def test_batch_parser_deduplicates_and_reports_bad_lines():
    entries, errors = parse_batch("# comment\nA@Example.com|old|host:8080\n"
                                  "b@example.com|pass|socks5://host:1080\n"
                                  "a@example.com|new|\ninvalid\n")
    assert len(entries) == 2
    assert entries[0]["password"] == "new"
    assert entries[0]["proxy_url"] == ""
    assert entries[1]["proxy_url"] == "socks5://host:1080"
    assert errors == [{"line": 5, "message": "格式须为 邮箱|密码|代理，代理可留空"}]


def test_password_is_encrypted_and_login_updates_session(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("KR_DATA_DIR", str(tmp_path))
    store = Store(str(tmp_path / "accounts.db"))
    ciphertext = encrypt_password("private-password")
    assert "private-password" not in ciphertext
    assert decrypt_password(ciphertext) == "private-password"
    account = store.upsert_account(name="a@example.com", password_ciphertext=ciphertext)
    assert account["has_password"] and account["login_status"] == "login_pending"
    assert "password" not in account and "password_ciphertext" not in account
    service = Service(store)
    monkeypatch.setattr("app.service.browser_login", lambda *args: (
        [{"name": "sb-superb-auth-token", "value": "session", "domain": ".krea.ai"}],
        "Chrome", "project-one", 100.0))
    class Client:
        def balance(self):
            return 99.0
    monkeypatch.setattr(service, "_client", lambda _: Client())
    service._login_account(account["id"])
    updated = store.account(account["id"])
    assert updated["login_status"] == "ready"
    assert updated["project_id"] == "project-one"
    assert updated["balance"] == 99.0
    assert updated["enabled"] is False
    service.stop()
