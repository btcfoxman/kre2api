import pytest

from app.client import KreaError
from app.service import INSUFFICIENT_CREDITS_MESSAGE, Service
from app.store import LOW_BALANCE_MESSAGE, Store


REQUEST = {"model": "sd-2-0-fast", "prompt": "A landscape", "duration": 5,
           "resolution": "720p"}


def account(store, name):
    return store.upsert_account(name=name,
                                cookies=[{"name": "session", "value": name}],
                                project_id="project")


def mock_clients(service, monkeypatch, balances, quotes, submitted):
    calls = {account_id: 0 for account_id in balances}
    balance_calls = {account_id: 0 for account_id in balances}

    class Client:
        def __init__(self, selected):
            self.selected = selected

        def balance(self):
            account_id = self.selected["id"]
            values = balances[account_id]
            if isinstance(values, list):
                index = balance_calls[account_id]
                balance_calls[account_id] += 1
                return values[min(index, len(values) - 1)]
            return values

        def estimate(self, _normalized, *, video_seconds=0):
            index = calls[self.selected["id"]]
            calls[self.selected["id"]] += 1
            values = quotes[self.selected["id"]]
            value = values[min(index, len(values) - 1)]
            if isinstance(value, Exception):
                raise value
            return value

        def prepare_media(self, normalized):
            return normalized, 0.0

        def export_cookies(self):
            return self.selected["cookies"]

        def generation_input(self, _normalized, _project_id):
            return {"provider": "test"}

        def submit(self, _normalized, _project_id):
            submitted.append(self.selected["id"])
            return [{"job_id": "upstream-job"}]

    monkeypatch.setattr(service, "_client", lambda selected: Client(selected))
    monkeypatch.setattr(service, "_enqueue", lambda _task_id: None)


def test_low_balance_account_is_disabled_and_next_account_is_selected(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "selection.db"))
    low, funded = account(store, "low"), account(store, "funded")
    service = Service(store)
    mock_clients(service, monkeypatch, {low["id"]: 149.99, funded["id"]: 500},
                 {low["id"]: [200], funded["id"]: [200]}, [])
    try:
        task = service.create(REQUEST)
        assert task["account_id"] == funded["id"]
        assert store.account(low["id"])["enabled"] is False
        assert store.account(low["id"])["last_error"] == LOW_BALANCE_MESSAGE
    finally:
        service.stop()


def test_all_accounts_insufficient_returns_402_without_creating_task(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "insufficient.db"))
    low, short = account(store, "low"), account(store, "short")
    service = Service(store)
    mock_clients(service, monkeypatch, {low["id"]: 149, short["id"]: 180},
                 {low["id"]: [200], short["id"]: [200]}, [])
    try:
        with pytest.raises(KreaError) as error:
            service.create(REQUEST)
        assert error.value.status_code == 402
        assert error.value.code == "INSUFFICIENT_CREDITS"
        assert str(error.value) == INSUFFICIENT_CREDITS_MESSAGE
        assert store.tasks() == []
        assert store.account(low["id"])["enabled"] is False
        assert store.account(short["id"])["enabled"] is True
    finally:
        service.stop()


def test_all_previously_disabled_low_balance_accounts_return_402(tmp_path):
    store = Store(str(tmp_path / "disabled.db"))
    item = account(store, "low")
    store.set_account(item["id"], balance=100)
    service = Service(store)
    try:
        with pytest.raises(KreaError) as error:
            service.create(REQUEST)
        assert error.value.status_code == 402
        assert error.value.code == "INSUFFICIENT_CREDITS"
    finally:
        service.stop()


def test_all_accounts_busy_returns_queue_full_instead_of_credit_error(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "busy.db"))
    item = account(store, "busy")
    assert store.reserve_task(task_id="pending", account_id=item["id"],
                              model="sd-2-0-fast", request=REQUEST, normalized={},
                              estimated_cost=200, balance=500)
    service = Service(store)
    mock_clients(service, monkeypatch, {item["id"]: 500}, {item["id"]: [200]}, [])
    try:
        with pytest.raises(KreaError) as error:
            service.create(REQUEST)
        assert error.value.status_code == 503
        assert error.value.code == "TASK_QUEUE_FULL"
    finally:
        service.stop()


def test_post_upload_cost_increase_moves_task_before_submission(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "reassignment.db"))
    first, second = account(store, "first"), account(store, "second")
    service = Service(store)
    submitted = []
    mock_clients(service, monkeypatch, {first["id"]: 300, second["id"]: 600},
                 {first["id"]: [200, 400], second["id"]: [400]}, submitted)
    try:
        task = service.create(REQUEST)
        assert task["account_id"] == first["id"]
        service._process(task["id"])
        stored = store.task(task["id"])
        assert stored["account_id"] == second["id"]
        assert stored["status"] == "running"
        assert stored["estimated_cost"] == 400
        assert submitted == [second["id"]]
    finally:
        service.stop()


def test_post_upload_balance_drop_disables_account_and_moves_task(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "balance_drop.db"))
    first, second = account(store, "first"), account(store, "second")
    service = Service(store)
    submitted = []
    mock_clients(service, monkeypatch, {first["id"]: [300, 100], second["id"]: 600},
                 {first["id"]: [200, 200], second["id"]: [200]}, submitted)
    try:
        task = service.create(REQUEST)
        service._process(task["id"])
        assert store.task(task["id"])["account_id"] == second["id"]
        assert store.account(first["id"])["enabled"] is False
        assert store.account(first["id"])["last_error"] == LOW_BALANCE_MESSAGE
        assert submitted == [second["id"]]
    finally:
        service.stop()


def test_estimate_402_after_upload_moves_task(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "estimate_402.db"))
    first, second = account(store, "first"), account(store, "second")
    service = Service(store)
    submitted = []
    mock_clients(service, monkeypatch, {first["id"]: 300, second["id"]: 600},
                 {first["id"]: [200, KreaError("upstream insufficient", 402)],
                  second["id"]: [200]}, submitted)
    try:
        task = service.create(REQUEST)
        service._process(task["id"])
        assert store.task(task["id"])["account_id"] == second["id"]
        assert store.task(task["id"])["status"] == "running"
        assert submitted == [second["id"]]
    finally:
        service.stop()


def test_pinned_account_is_not_reassigned(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "pinned.db"))
    first, second = account(store, "first"), account(store, "second")
    service = Service(store)
    submitted = []
    mock_clients(service, monkeypatch, {first["id"]: 300, second["id"]: 600},
                 {first["id"]: [200, 400], second["id"]: [400]}, submitted)
    try:
        task = service.create({**REQUEST, "account_id": first["id"]})
        service._process(task["id"])
        stored = store.task(task["id"])
        assert stored["account_id"] == first["id"]
        assert stored["status"] == "failed"
        assert stored["error"] == INSUFFICIENT_CREDITS_MESSAGE
        assert service.public_task(stored)["error_code"] == "INSUFFICIENT_CREDITS"
        assert submitted == []
    finally:
        service.stop()
