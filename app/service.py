"""Account selection and durable Krea video task processing."""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .catalog import normalize_request
from .client import KreaClient, KreaError, TERMINAL_STATUSES
from .browser_login import KreaLoginError, login as browser_login, new_project_id
from .credentials import decrypt_password
from .manual_browser import ManualBrowser
from .store import Store


LOG = logging.getLogger("kre2api.service")
FINAL = {"succeeded", "failed", "expired"}


def result_urls(job: dict[str, Any]) -> list[str]:
    result = job.get("result") or {}
    urls: list[str] = []
    for source in (result, result.get("data") if isinstance(result, dict) else None):
        if not isinstance(source, dict):
            continue
        for key in ("image_urls", "video_urls", "urls", "videos"):
            for item in source.get(key) or []:
                value = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
                if isinstance(value, str) and value.startswith(("https://", "http://")):
                    urls.append(value)
    return list(dict.fromkeys(urls))


def failure_message(job: dict[str, Any], *, refund_confirmed: bool = False) -> str:
    result = job.get("result") or {}
    nested = result.get("data") if isinstance(result, dict) else None
    nested = nested if isinstance(nested, dict) else {}
    details: list[dict[str, Any]] = []
    raw_message = nested.get("msg")
    if isinstance(raw_message, str):
        try:
            body = json.loads(raw_message)
            if isinstance(body, dict) and isinstance(body.get("detail"), list):
                details = [item for item in body["detail"] if isinstance(item, dict)]
        except ValueError:
            pass
    policy = nested.get("type") == "content_policy_violation" or any(
        item.get("type") == "content_policy_violation" for item in details)
    if policy:
        output = any("generated_video" in (item.get("loc") or []) or
                     str(item.get("msg") or "").lower().startswith("output ")
                     for item in details)
        if output:
            return ("生成的视频内容违规，请修改描述后重试，积分已返还~" if refund_confirmed
                    else "生成的视频内容违规，请修改描述后重试")
        return ("检测到内容有敏感或违规情况，积分已返还，请重试~" if refund_confirmed
                else "检测到内容有敏感或违规情况，请修改后重试")
    for value in (job.get("error"), job.get("message"),
                  result.get("error") if isinstance(result, dict) else None,
                  result.get("message") if isinstance(result, dict) else None,
                  nested.get("error")):
        if isinstance(value, dict):
            value = value.get("message") or value.get("description")
        if value:
            return str(value)[:600]
    return f"Krea task {job.get('status') or 'failed'}"


class Service:
    def __init__(self, store: Store, *, workers: int = 4,
                 poll_seconds: int = 10, timeout_seconds: int = 3600) -> None:
        self.store = store
        saved = store.settings()
        self.poll_seconds = int(saved.get("poll_interval_seconds", max(2, poll_seconds)))
        self.timeout_seconds = int(saved.get("task_timeout_seconds", max(60, timeout_seconds)))
        self.workers = max(1, workers)
        self.executor = ThreadPoolExecutor(max_workers=self.workers,
                                           thread_name_prefix="kre2api")
        self.stop_event = threading.Event()
        self.poll_thread: threading.Thread | None = None
        self.inflight: set[str] = set()
        self.lock = threading.Lock()
        self.login_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kre-login")
        self.login_inflight: set[int] = set()
        self.manual_browsers: dict[int, ManualBrowser] = {}

    def start(self) -> None:
        for account in self.store.accounts():
            if account["enabled"] and account["has_password"] and account["login_status"] in {"login_pending", "logging_in"}:
                self.schedule_login(account["id"])
        for task in self.store.pending_tasks():
            if task["status"] == "submitting" and not task["upstream_job_id"]:
                self.store.update_task(task["id"], status="failed", reserved_cost=0,
                                       error="submission outcome unknown after restart; review upstream jobs")
            elif not task["upstream_job_id"]:
                self._enqueue(task["id"])
        self.poll_thread = threading.Thread(target=self._poll_loop, name="kre2api-poller",
                                            daemon=True)
        self.poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        for account_id in list(self.manual_browsers):
            self.close_manual_browser(account_id)
        if self.poll_thread:
            self.poll_thread.join(timeout=3)
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.login_executor.shutdown(wait=False, cancel_futures=True)

    def schedule_login(self, account_id: int) -> bool:
        with self.lock:
            account = self.store.account(account_id)
            if (not account or not account["enabled"] or not account["has_password"]
                    or account_id in self.login_inflight or account_id in self.manual_browsers):
                return False
            self.login_inflight.add(account_id)
            self.store.set_account(account_id, login_status="login_pending", last_error="")
        try:
            future = self.login_executor.submit(self._login_account, account_id)
            future.add_done_callback(lambda _: self._finish_login(account_id))
            return True
        except Exception:
            self._finish_login(account_id)
            raise

    def _finish_login(self, account_id: int) -> None:
        with self.lock:
            self.login_inflight.discard(account_id)

    def open_manual_browser(self, account_id: int) -> dict[str, Any]:
        with self.lock:
            account = self.store.account(account_id)
            if not account:
                raise ValueError("account not found")
            if account_id in self.login_inflight:
                raise KreaLoginError("Automatic login is still running; retry shortly", "login_pending")
            browser = self.manual_browsers.get(account_id)
            if browser is None:
                browser = ManualBrowser(account_id, account["proxy_url"],
                                        Path(os.getenv("KR_DATA_DIR", "/app/data")))
                self.manual_browsers[account_id] = browser
        return browser.snapshot()

    def manual_browser_snapshot(self, account_id: int) -> dict[str, Any]:
        browser = self.manual_browsers.get(account_id)
        if browser is None:
            raise ValueError("account browser is not open")
        return browser.snapshot()

    def manual_browser_action(self, account_id: int, action: dict[str, Any]) -> dict[str, Any]:
        browser = self.manual_browsers.get(account_id)
        account = self.store.account(account_id)
        if browser is None or account is None:
            raise ValueError("account browser is not open")
        password = decrypt_password(self.store.credential(account_id)) if action.get("action") == "login" else ""
        return {"action_result": browser.action(action, account["name"], password)}

    def complete_manual_browser(self, account_id: int) -> dict[str, Any]:
        browser = self.manual_browsers.get(account_id)
        account = self.store.account(account_id)
        if browser is None or account is None:
            raise ValueError("account browser is not open")
        cookies, user_agent, project = browser.capture(account["name"])
        balance = KreaClient(cookies=cookies, proxy_url=account["proxy_url"],
                             user_agent=user_agent).balance()
        project = project or account["project_id"] or new_project_id()
        self.store.set_account(account_id, cookies=cookies, user_agent=user_agent,
                               project_id=project, balance=balance,
                               login_status="ready" if project else "project_required",
                               last_error="" if project else "Create a Krea video project and set its ID")
        self.close_manual_browser(account_id)
        return self.store.account(account_id)

    def close_manual_browser(self, account_id: int) -> None:
        with self.lock:
            browser = self.manual_browsers.pop(account_id, None)
        if browser:
            browser.close()

    def _login_account(self, account_id: int) -> None:
        account = self.store.account(account_id)
        if not account or not account["enabled"]:
            return
        self.store.set_account(account_id, login_status="logging_in")
        try:
            password = decrypt_password(self.store.credential(account_id))
            data_dir = Path(os.getenv("KR_DATA_DIR", "/app/data"))
            cookies, user_agent, project, balance = browser_login(
                account_id, account["name"], password, account["proxy_url"], data_dir)
            candidate = {**account, "cookies": cookies, "user_agent": user_agent}
            client = self._client(candidate)
            verified_balance = client.balance()
            self.store.set_account(account_id, cookies=cookies, user_agent=user_agent,
                                   project_id=account["project_id"] or project,
                                   balance=verified_balance if verified_balance is not None else balance,
                                   login_status="ready" if account["project_id"] or project else "project_required",
                                   last_error="" if account["project_id"] or project else "Create a Krea video project and set its ID")
        except KreaLoginError as exc:
            self.store.set_account(account_id, login_status=exc.status, last_error=str(exc))
        except KreaError as exc:
            status = "session_required" if exc.status_code in {401, 403} else "network_error" if exc.retryable or exc.status_code >= 500 else "login_failed"
            self.store.set_account(account_id, login_status=status,
                                   last_error=f"Krea session check failed (HTTP {exc.status_code})")
        except Exception as exc:
            self.store.set_account(account_id, login_status="login_failed",
                                   last_error=f"Krea login failed: {type(exc).__name__}")

    @staticmethod
    def _client(account: dict[str, Any]) -> KreaClient:
        return KreaClient(cookies=account["cookies"],
                          proxy_url=account["proxy_url"],
                          user_agent=account["user_agent"])

    def _learned_estimate(self, normalized: dict[str, Any], quote: float,
                          video_seconds: float) -> float:
        ratios = []
        for sample in self.store.cost_samples(200):
            if (sample["model"] == normalized["model"]
                    and sample["duration"] == normalized["duration"]
                    and sample["resolution"] == normalized["resolution"]
                    and sample["video_count"] == len(normalized["videos"])
                    and abs(sample["video_reference_seconds"] - video_seconds) <= 1
                    and sample["estimated_cost"] > 0):
                ratios.append(sample["actual_cost"] / sample["estimated_cost"])
        if not ratios:
            return quote
        return max(quote, quote * min(2.0, max(1.0, statistics.median(ratios))))

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_request(request)
        accounts = [account for account in self.store.accounts(enabled_only=True)
                    if account.get("project_id") and account.get("cookies")
                    and account.get("login_status") == "ready"]
        if requested_account := request.get("account_id"):
            accounts = [account for account in accounts if account["id"] == int(requested_account)]
        if not accounts:
            raise KreaError("no enabled Krea account with a project is configured", 503)
        candidate_errors: list[str] = []
        task_id = "kre_" + uuid.uuid4().hex[:16]
        video_seconds = sum(float(item.get("duration") or 0) for item in normalized["videos"])
        for account in sorted(accounts, key=lambda item: -(item.get("balance") or 0)):
            try:
                client = self._client(account)
                balance = client.balance()
                quote = client.estimate(normalized, video_seconds=video_seconds)
                cost = self._learned_estimate(normalized, quote, video_seconds)
                self.store.set_account(account["id"], balance=balance,
                                       cookies=client.export_cookies(), last_error="")
                if self.store.reserve_task(task_id=task_id, account_id=account["id"],
                                           model=normalized["model"], request=request,
                                           normalized=normalized, estimated_cost=cost,
                                           balance=balance):
                    self._enqueue(task_id)
                    return self.public_task(self.store.task(task_id))
                candidate_errors.append(f"{account['name']}: insufficient available compute units or busy")
            except KreaError as exc:
                self.store.set_account(account["id"], last_error=str(exc))
                candidate_errors.append(f"{account['name']}: {exc}")
        raise KreaError("; ".join(candidate_errors)[:1000] or "no Krea account available", 503)

    def _enqueue(self, task_id: str) -> None:
        with self.lock:
            if task_id in self.inflight:
                return
            self.inflight.add(task_id)
        future = self.executor.submit(self._process, task_id)
        future.add_done_callback(lambda _: self.inflight.discard(task_id))

    def _process(self, task_id: str) -> None:
        task = self.store.task(task_id)
        if not task or task["status"] in FINAL:
            return
        account = self.store.account(task["account_id"])
        if not account:
            self.store.update_task(task_id, status="failed", reserved_cost=0,
                                   error="assigned account no longer exists")
            return
        client = self._client(account)
        try:
            self.store.update_task(task_id, status="preparing")
            normalized, video_seconds = client.prepare_media(task["normalized"])
            quote = client.estimate(normalized, video_seconds=video_seconds)
            balance = client.balance()
            cost = self._learned_estimate(normalized, quote, video_seconds)
            if not self.store.adjust_reservation(task_id, cost, balance):
                raise KreaError("account compute units became insufficient after media upload", 402)
            self.store.update_task(task_id, normalized_json=json.dumps(normalized, ensure_ascii=False))
            upstream_request = {"method": "POST", "path": "/api/jobs/v2/new/videoV2",
                                "payload": client.generation_input(normalized, account["project_id"])}
            self.store.update_task(task_id, upstream_request_json=json.dumps(upstream_request, ensure_ascii=False))
            self.store.update_task(task_id, status="submitting")
            jobs = client.submit(normalized, account["project_id"])
            upstream_id = str(jobs[0]["job_id"])
            self.store.update_task(task_id, upstream_job_id=upstream_id,
                                   upstream_response_json=json.dumps({"submission": jobs}, ensure_ascii=False),
                                   status="running", error="")
            self.store.set_account(account["id"], cookies=client.export_cookies(),
                                   last_error="")
        except Exception as exc:
            message = str(exc)[:600]
            self.store.update_task(task_id, status="failed", reserved_cost=0,
                                   error=message)
            self.store.set_account(account["id"], last_error=message,
                                   cookies=client.export_cookies())
            LOG.warning("task %s failed before upstream acceptance: %s", task_id, message)

    def _poll_loop(self) -> None:
        while not self.stop_event.wait(self.poll_seconds):
            for task in self.store.pending_tasks():
                if not task["upstream_job_id"] or task["status"] in {"queued", "preparing", "submitting"}:
                    continue
                if time.time() - task["created_at"] > self.timeout_seconds:
                    self.store.update_task(task["id"], status="expired", reserved_cost=0,
                                           error="Krea task polling timed out")
                    continue
                try:
                    self._poll_one(task)
                except Exception as exc:
                    LOG.warning("task %s poll failed: %s", task["id"], str(exc)[:300])

    def _poll_one(self, task: dict[str, Any]) -> None:
        account = self.store.account(task["account_id"])
        if not account:
            return
        client = self._client(account)
        job = client.job(task["upstream_job_id"])
        self.store.update_task(task["id"], upstream_response_json=json.dumps({
            **task.get("upstream_response", {}), "latest_status": job,
        }, ensure_ascii=False))
        status = str(job.get("status") or "")
        if status not in TERMINAL_STATUSES:
            self.store.update_task(task["id"], status="running")
            self.store.set_account(account["id"], cookies=client.export_cookies())
            return
        balance_after = client.balance()
        observed = max(0.0, float(task.get("balance_before") or 0) - balance_after)
        estimate = float(task.get("estimated_cost") or 0)
        actual = (observed if not self.store.task_overlapped(task)
                  and 0.5 * estimate <= observed <= 1.5 * estimate else None)
        if status == "completed":
            urls = result_urls(job)
            if urls:
                self.store.update_task(task["id"], status="succeeded",
                                       result_json=json.dumps(urls),
                                       reserved_cost=0, actual_cost=actual,
                                       balance_after=balance_after, error="")
                refreshed = self.store.task(task["id"])
                video_seconds = sum(float(item.get("duration") or 0)
                                    for item in refreshed["normalized"]["videos"])
                if actual is not None:
                    self.store.record_cost(refreshed, actual, video_seconds)
            else:
                self.store.update_task(task["id"], status="failed", reserved_cost=0,
                                       balance_after=balance_after,
                                       error="Krea completed without a video URL")
        else:
            unchanged_balance = (task.get("balance_before") is not None
                                 and abs(float(task["balance_before"]) - balance_after) < 0.001
                                 and not self.store.task_overlapped(task))
            self.store.update_task(task["id"], status="failed", reserved_cost=0,
                                   balance_after=balance_after,
                                   error=failure_message(job, refund_confirmed=unchanged_balance))
        self.store.set_account(account["id"], balance=balance_after,
                               cookies=client.export_cookies())

    @staticmethod
    def public_task(task: dict[str, Any]) -> dict[str, Any]:
        normalized = task.get("normalized") or {}
        return {
            "id": task["id"], "object": "video", "created_at": int(task["created_at"]),
            "status": task["status"], "model": task["model"],
            "duration": normalized.get("duration"),
            "resolution": normalized.get("resolution"),
            "aspect_ratio": normalized.get("aspect_ratio"),
            "account_id": task["account_id"],
            "estimated_cost": task["estimated_cost"],
            "actual_cost": task["actual_cost"],
            "data": [{"url": url} for url in task["result_urls"]],
            "error": task["error"] or None,
        }
