from app.store import Store
import sqlite3


def test_account_balance_reservation_and_cost_sample(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    account = store.upsert_account(name="demo", cookies=[{"name": "session", "value": "secret"}],
                                   project_id="project")
    normalized = {"model": "sd-2-0-mini", "provider": "seedance-2-mini",
                  "duration": 5, "resolution": "480p", "images": [], "videos": [], "audios": []}
    assert store.reserve_task(task_id="kre_one", account_id=account["id"], model="sd-2-0-mini",
                              request={"prompt": "test"}, normalized=normalized,
                              estimated_cost=152.05, balance=500)
    assert not store.reserve_task(task_id="kre_two", account_id=account["id"], model="sd-2-0-mini",
                                  request={"prompt": "test"}, normalized=normalized,
                                  estimated_cost=152.05, balance=500)
    store.update_task("kre_one", status="succeeded", reserved_cost=0,
                      actual_cost=152.048, result_json='["https://example.test/video.mp4"]')
    task = store.task("kre_one")
    assert task["result_urls"] == ["https://example.test/video.mp4"]
    store.record_cost(task, 152.048)
    assert store.cost_samples()[0]["actual_cost"] == 152.048
    assert store.reserve_task(task_id="kre_two", account_id=account["id"], model="sd-2-0-mini",
                              request={"prompt": "test"}, normalized=normalized,
                              estimated_cost=152.05, balance=347.952)


def test_existing_task_table_gains_upstream_audit_columns(tmp_path):
    path = tmp_path / "existing.db"
    Store(str(path))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN upstream_request_json")
        db.execute("ALTER TABLE tasks DROP COLUMN upstream_response_json")
    migrated = Store(str(path))
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    assert {"upstream_request_json", "upstream_response_json"} <= columns
    assert migrated.settings() == {}
