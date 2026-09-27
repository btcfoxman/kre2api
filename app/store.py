"""SQLite state for accounts, tasks, reservations and observed prices."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    cookies_json TEXT NOT NULL,
                    password_ciphertext TEXT NOT NULL DEFAULT '',
                    login_status TEXT NOT NULL DEFAULT 'session_required',
                    proxy_url TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    max_concurrency INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    balance REAL,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    upstream_job_id TEXT NOT NULL DEFAULT '',
                    estimated_cost REAL NOT NULL,
                    reserved_cost REAL NOT NULL,
                    actual_cost REAL,
                    balance_before REAL,
                    balance_after REAL,
                    result_json TEXT NOT NULL DEFAULT '[]',
                    upstream_request_json TEXT NOT NULL DEFAULT '{}',
                    upstream_response_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_account_status ON tasks(account_id, status);
                CREATE INDEX IF NOT EXISTS tasks_upstream_id ON tasks(upstream_job_id);
                CREATE TABLE IF NOT EXISTS cost_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    duration INTEGER NOT NULL,
                    resolution TEXT NOT NULL,
                    video_reference_seconds REAL NOT NULL,
                    image_count INTEGER NOT NULL,
                    video_count INTEGER NOT NULL,
                    audio_count INTEGER NOT NULL,
                    estimated_cost REAL NOT NULL,
                    actual_cost REAL NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cost_samples_model ON cost_samples(model, duration, resolution);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            for column in ("upstream_request_json", "upstream_response_json"):
                if column not in task_columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
            account_columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "password_ciphertext" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN password_ciphertext TEXT NOT NULL DEFAULT ''")
            if "login_status" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN login_status TEXT NOT NULL DEFAULT 'session_required'")
                db.execute("UPDATE accounts SET login_status='ready' WHERE cookies_json!='[]'")
            if "max_concurrency" not in account_columns:
                db.execute("ALTER TABLE accounts ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT 1")

    @staticmethod
    def _account(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["cookies"] = json.loads(result.pop("cookies_json"))
        result["has_password"] = bool(result.pop("password_ciphertext"))
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["normalized"] = json.loads(result.pop("normalized_json"))
        result["result_urls"] = json.loads(result.pop("result_json"))
        result["upstream_request"] = json.loads(result.pop("upstream_request_json"))
        result["upstream_response"] = json.loads(result.pop("upstream_response_json"))
        return result

    def upsert_account(self, *, name: str, cookies: list[dict[str, Any]] | None = None,
                       password_ciphertext: str | None = None, proxy_url: str | None = None,
                       user_agent: str | None = None, project_id: str | None = None,
                       enabled: bool = True, login_status: str | None = None,
                       max_concurrency: int | None = None) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("account name is required")
        if max_concurrency is not None and (isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 16):
            raise ValueError("max_concurrency must be between 1 and 16")
        now = time.time()
        with self._connect() as db:
            old = db.execute("SELECT * FROM accounts WHERE name=?", (name.strip(),)).fetchone()
            if not old and not (cookies or password_ciphertext):
                raise ValueError("browser cookies or password are required")
            saved_cookies = cookies if cookies is not None else json.loads(old["cookies_json"]) if old else []
            saved_password = password_ciphertext if password_ciphertext is not None else old["password_ciphertext"] if old else ""
            saved_status = login_status or ("ready" if cookies else old["login_status"] if old else "login_pending")
            db.execute("""
                INSERT INTO accounts(name,cookies_json,password_ciphertext,login_status,proxy_url,user_agent,project_id,max_concurrency,enabled,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                  cookies_json=excluded.cookies_json,password_ciphertext=excluded.password_ciphertext,
                  login_status=excluded.login_status,proxy_url=excluded.proxy_url,
                  user_agent=excluded.user_agent,project_id=excluded.project_id,
                  max_concurrency=excluded.max_concurrency,
                  enabled=excluded.enabled,last_error='',updated_at=excluded.updated_at
            """, (name.strip(), _json(saved_cookies), saved_password, saved_status,
                  proxy_url if proxy_url is not None else old["proxy_url"] if old else "",
                  user_agent if user_agent is not None else old["user_agent"] if old else "",
                  project_id if project_id is not None else old["project_id"] if old else "",
                  max_concurrency if max_concurrency is not None else old["max_concurrency"] if old else 1,
                  int(enabled), now))
            row = db.execute("SELECT * FROM accounts WHERE name=?", (name.strip(),)).fetchone()
        return self._account(row)

    def credential(self, account_id: int) -> str:
        with self._connect() as db:
            row = db.execute("SELECT password_ciphertext FROM accounts WHERE id=?", (account_id,)).fetchone()
        return str(row[0]) if row else ""

    def accounts(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM accounts WHERE (?=0 OR enabled=1) ORDER BY id",
                              (int(enabled_only),)).fetchall()
        return [self._account(row) for row in rows]

    def account(self, account_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row) if row else None

    def set_account(self, account_id: int, *, balance: float | None = None,
                    cookies: list[dict[str, Any]] | None = None,
                    last_error: str | None = None, enabled: bool | None = None,
                    proxy_url: str | None = None,
                    project_id: str | None = None,
                    user_agent: str | None = None,
                    login_status: str | None = None,
                    max_concurrency: int | None = None) -> None:
        changes: dict[str, Any] = {"updated_at": time.time()}
        if balance is not None:
            changes["balance"] = balance
        if cookies is not None:
            changes["cookies_json"] = _json(cookies)
        if last_error is not None:
            changes["last_error"] = last_error[:600]
        if enabled is not None:
            changes["enabled"] = int(enabled)
        if proxy_url is not None:
            changes["proxy_url"] = proxy_url
        if project_id is not None:
            changes["project_id"] = project_id
        if user_agent is not None:
            changes["user_agent"] = user_agent
        if login_status is not None:
            changes["login_status"] = login_status
        if max_concurrency is not None:
            if isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 16:
                raise ValueError("max_concurrency must be between 1 and 16")
            changes["max_concurrency"] = max_concurrency
        sql = "UPDATE accounts SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?"
        with self._connect() as db:
            db.execute(sql, (*changes.values(), account_id))

    def reserve_task(self, *, task_id: str, account_id: int, model: str,
                     request: dict[str, Any], normalized: dict[str, Any],
                     estimated_cost: float, balance: float) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT COUNT(*) FROM tasks WHERE account_id=? AND status NOT IN ('succeeded','failed','expired')",
                                (account_id,)).fetchone()[0]
            limit_row = db.execute("SELECT max_concurrency FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not limit_row or active >= limit_row[0]:
                return False
            reserved = db.execute("SELECT COALESCE(SUM(reserved_cost),0) FROM tasks WHERE account_id=? AND status NOT IN ('succeeded','failed','expired')",
                                  (account_id,)).fetchone()[0]
            if float(balance) - float(reserved) < float(estimated_cost):
                return False
            db.execute("""
                INSERT INTO tasks(id,account_id,model,status,request_json,normalized_json,
                                  estimated_cost,reserved_cost,balance_before,created_at,updated_at)
                VALUES(?,?,?,'queued',?,?,?,?,?,?,?)
            """, (task_id, account_id, model, _json(request), _json(normalized),
                  estimated_cost, estimated_cost, balance, now, now))
            db.execute("UPDATE accounts SET balance=?,updated_at=? WHERE id=?",
                       (balance, now, account_id))
        return True

    def adjust_reservation(self, task_id: str, new_cost: float, balance: float) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return False
            reserved = db.execute("SELECT COALESCE(SUM(reserved_cost),0) FROM tasks WHERE account_id=? AND id<>? AND status NOT IN ('succeeded','failed','expired')",
                                  (row[0], task_id)).fetchone()[0]
            if balance - float(reserved) < new_cost:
                return False
            db.execute("UPDATE tasks SET estimated_cost=?,reserved_cost=?,updated_at=? WHERE id=?",
                       (new_cost, new_cost, time.time(), task_id))
            return True

    def task_overlapped(self, task: dict[str, Any]) -> bool:
        with self._connect() as db:
            row = db.execute("""SELECT COUNT(*) FROM tasks
                                WHERE account_id=? AND id!=? AND created_at<=?
                                  AND (status NOT IN ('succeeded','failed','expired')
                                       OR updated_at>=?)""",
                             (task["account_id"], task["id"], time.time(),
                              task["created_at"])).fetchone()
        return bool(row[0])

    def task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._task(row) for row in rows]

    def pending_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tasks WHERE status NOT IN ('succeeded','failed','expired') ORDER BY created_at").fetchall()
        return [self._task(row) for row in rows]

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "upstream_job_id", "estimated_cost", "reserved_cost",
                   "actual_cost", "balance_after", "result_json", "error", "normalized_json",
                   "upstream_request_json", "upstream_response_json"}
        if unknown := set(fields) - allowed:
            raise ValueError(f"unknown task fields: {unknown}")
        fields["updated_at"] = time.time()
        sql = "UPDATE tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?"
        with self._connect() as db:
            db.execute(sql, (*fields.values(), task_id))

    def record_cost(self, task: dict[str, Any], actual: float,
                    video_reference_seconds: float = 0) -> None:
        data = task["normalized"]
        with self._connect() as db:
            db.execute("""
                INSERT OR REPLACE INTO cost_samples(
                    task_id,model,provider,duration,resolution,video_reference_seconds,
                    image_count,video_count,audio_count,estimated_cost,actual_cost,observed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """, (task["id"], task["model"], data["provider"], data["duration"],
                  data["resolution"], video_reference_seconds, len(data["images"]),
                  len(data["videos"]), len(data["audios"]), task["estimated_cost"],
                  actual, time.time()))

    def cost_samples(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cost_samples ORDER BY observed_at DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def settings(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key,value FROM settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}

    def set_settings(self, values: dict[str, Any]) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, _json(value)) for key, value in values.items()],
            )

    def clear_finished_tasks(self) -> int:
        with self._connect() as db:
            result = db.execute("DELETE FROM tasks WHERE status IN ('succeeded','failed','expired')")
        return result.rowcount
