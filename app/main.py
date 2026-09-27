"""FastAPI surface for Krea video generation and account management."""

from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .catalog import normalize_request, public_models
from .image_catalog import is_image_model
from .batch import parse_batch
from .browser_login import KreaLoginError
from .client import KreaClient, KreaError
from .credentials import encrypt_password
from .service import Service
from .store import MIN_ACCOUNT_BALANCE, Store


API_KEY = os.getenv("KR_API_KEY", "")
ADMIN_TOKEN = os.getenv("KR_ADMIN_TOKEN", "")
SYNC_TOKEN = os.getenv("KR_SYNC_TOKEN", "")
if not API_KEY or not ADMIN_TOKEN:
    raise RuntimeError("KR_API_KEY and KR_ADMIN_TOKEN must be configured")

DATA_DIR = Path(os.getenv("KR_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = Store(os.getenv("KR_DATABASE_PATH", str(DATA_DIR / "kre2api.db")))
service = Service(store, workers=int(os.getenv("KR_TASK_WORKERS", "10")),
                  poll_seconds=int(os.getenv("KR_POLL_INTERVAL_SECONDS", "10")),
                  timeout_seconds=int(os.getenv("KR_TASK_TIMEOUT_SECONDS", "3600")))
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(title="KRE2API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _token(authorization: str | None, x_api_key: str | None) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return str(x_api_key or "").strip()


def api_auth(authorization: str | None = Header(default=None),
             x_api_key: str | None = Header(default=None)) -> None:
    if not secrets.compare_digest(_token(authorization, x_api_key), API_KEY):
        raise HTTPException(status_code=401, detail="invalid API key")


def sync_auth(authorization: str | None = Header(default=None),
              x_api_key: str | None = Header(default=None)) -> None:
    value = _token(authorization, x_api_key)
    if not any(secrets.compare_digest(value, allowed) for allowed in
               (API_KEY, SYNC_TOKEN) if allowed):
        raise HTTPException(status_code=401, detail="invalid sync token")


def admin_auth(request: Request, kr_admin: str | None = Cookie(default=None),
               x_admin_token: str | None = Header(default=None)) -> None:
    value = x_admin_token or kr_admin or ""
    if not secrets.compare_digest(value, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="admin login required")


def _raise(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, KreaError):
        return HTTPException(status_code=exc.status_code, detail=str(exc),
                             headers={"X-KREAPI-Error-Code": exc.code} if exc.code else None)
    if isinstance(exc, KreaLoginError):
        return HTTPException(status_code=409 if exc.status == "challenge_required" else 503,
                             detail=str(exc))
    if isinstance(exc, (ValueError, TypeError, IndexError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="internal task error")


def _task(task_id: str) -> dict[str, Any]:
    result = store.task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="task not found")
    return result


def _create(body: dict[str, Any], wait: bool = False) -> dict[str, Any]:
    try:
        task = service.create(body)
    except Exception as exc:
        error = _raise(exc)
        if isinstance(exc, (KreaError, ValueError, TypeError, IndexError)):
            error.headers = {**(error.headers or {}), "X-KREAPI-Submit-Outcome": "not-accepted"}
        raise error from exc
    if wait:
        deadline = time.monotonic() + min(int(os.getenv("KR_SYNC_TIMEOUT_SECONDS", "900")), 900)
        while time.monotonic() < deadline:
            current = _task(task["id"])
            if current["status"] in {"succeeded", "failed", "expired"}:
                return service.public_task(current)
            time.sleep(2)
    return task


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "kre2api", "models": len(public_models())}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/admin/login")
def admin_login(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    if not secrets.compare_digest(str(body.get("token") or ""), ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="invalid admin token")
    response = JSONResponse({"ok": True})
    response.set_cookie("kr_admin", ADMIN_TOKEN, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=30 * 24 * 3600)
    return response


@app.post("/api/admin/logout", dependencies=[Depends(admin_auth)])
def admin_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("kr_admin")
    return response


@app.get("/v1/models", dependencies=[Depends(api_auth)])
def models() -> dict[str, Any]:
    return {"object": "list", "data": public_models()}


@app.post("/v1/videos", dependencies=[Depends(api_auth)])
@app.post("/v1/videos/generations", dependencies=[Depends(api_auth)])
def create_video(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body, wait=body.get("background") is False)


@app.post("/v1/images", dependencies=[Depends(api_auth)])
def create_image(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    value = {**body, "model": body.get("model") or "seedream-5.0-pro"}
    if not is_image_model(value["model"]):
        raise HTTPException(status_code=422, detail="an image model is required")
    return _create(value, wait=body.get("background") is False)


@app.post("/v1/images/generations", dependencies=[Depends(api_auth)])
def generate_images(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if body.get("background") is True:
        return create_image(body)
    task = create_image({**body, "background": False})
    if task["status"] != "succeeded":
        raise HTTPException(status_code=502 if task["status"] == "failed" else 504,
                            detail=task.get("error") or f"image task {task['id']} is not complete")
    return {"created": task["created_at"], "data": task["data"],
            "model": task["model"]}


@app.get("/v1/images/{task_id}", dependencies=[Depends(api_auth)])
def get_image(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    if task["normalized"].get("kind") != "image":
        raise HTTPException(status_code=404, detail="image task not found")
    return service.public_task(task)


@app.get("/v1/images/{task_id}/content", dependencies=[Depends(api_auth)])
def get_image_content(task_id: str) -> RedirectResponse:
    task = _task(task_id)
    if task["normalized"].get("kind") != "image" or task["status"] != "succeeded" or not task["result_urls"]:
        raise HTTPException(status_code=409, detail="image is not ready")
    return RedirectResponse(task["result_urls"][0], status_code=307)


@app.post("/api/admin/videos", dependencies=[Depends(admin_auth)])
def admin_create_video(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


@app.post("/api/admin/images", dependencies=[Depends(admin_auth)])
def admin_create_image(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return create_image(body)


@app.get("/v1/videos/{task_id}", dependencies=[Depends(api_auth)])
def get_video(task_id: str) -> dict[str, Any]:
    return service.public_task(_task(task_id))


@app.get("/v1/videos/{task_id}/content", dependencies=[Depends(api_auth)])
def get_video_content(task_id: str) -> RedirectResponse:
    task = _task(task_id)
    if task["status"] != "succeeded" or not task["result_urls"]:
        raise HTTPException(status_code=409, detail="video is not ready")
    return RedirectResponse(task["result_urls"][0], status_code=307)


@app.post("/api/v3/contents/generations/tasks", dependencies=[Depends(api_auth)])
def create_generic(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


@app.get("/api/v3/contents/generations/tasks/{task_id}", dependencies=[Depends(api_auth)])
def get_generic(task_id: str) -> dict[str, Any]:
    return service.public_task(_task(task_id))


@app.post("/v1/responses", dependencies=[Depends(api_auth)])
def create_response(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    value = dict(body)
    if isinstance(value.get("input"), str):
        value.setdefault("prompt", value["input"])
    task = _create(value, wait=body.get("background") is False)
    return _response(task)


def _response(task: dict[str, Any]) -> dict[str, Any]:
    kind = task.get("object") or "video"
    return {"id": task["id"], "object": "response", "model": task["model"],
            "status": "completed" if task["status"] == "succeeded" else task["status"],
            "output": [{"type": f"{kind}_generation_call", f"{kind}_url": item["url"]}
                       for item in task["data"]], "error": task["error"],
            "error_code": task.get("error_code")}


@app.get("/v1/responses/{task_id}", dependencies=[Depends(api_auth)])
def get_response(task_id: str) -> dict[str, Any]:
    return _response(service.public_task(_task(task_id)))


@app.post("/api/quote", dependencies=[Depends(api_auth)])
@app.post("/api/admin/quote", dependencies=[Depends(admin_auth)])
def quote(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        normalized = normalize_request(body)
        video_seconds = sum(float(item.get("duration") or 0) for item in normalized["videos"])
        output = []
        for account in store.accounts(enabled_only=True):
            if not account["cookies"] or account["login_status"] != "ready":
                continue
            if body.get("account_id") and account["id"] != int(body["account_id"]):
                continue
            try:
                client = service._client(account)
                balance = client.balance()
                store.set_account(account["id"], balance=balance,
                                  cookies=client.export_cookies(), last_error="")
                if balance < MIN_ACCOUNT_BALANCE:
                    output.append({"account_id": account["id"], "name": account["name"],
                                   "balance": balance, "estimated_cost": None,
                                   "eligible": False,
                                   "reason": "balance_below_150"})
                    continue
                quote = client.estimate(normalized, video_seconds=video_seconds,
                                        project_id=account["project_id"])
                cost = service._learned_estimate(normalized, quote, video_seconds)
                capacity = store.account_capacity(account["id"], balance)
                eligible = (capacity["enabled"] and capacity["available"] >= cost
                            and capacity["active"] < capacity["max_concurrency"])
                reason = ("insufficient_credits" if capacity["available"] < cost else
                          "busy" if capacity["active"] >= capacity["max_concurrency"] else
                          "disabled" if not capacity["enabled"] else "")
                output.append({"account_id": account["id"], "name": account["name"],
                               "balance": balance, "estimated_cost": cost,
                               "upstream_quote": quote,
                               "reserved_cost": capacity["reserved"],
                               "available_balance": capacity["available"],
                               "available_after": capacity["available"] - cost,
                               "active_tasks": capacity["active"],
                               "eligible": eligible, "reason": reason})
            except Exception as exc:
                output.append({"account_id": account["id"], "name": account["name"],
                               "error": str(exc)[:300], "eligible": False})
        return {"model": normalized["model"], "kind": normalized.get("kind", "video"),
                "duration": normalized["duration"],
                "resolution": normalized["resolution"],
                "width": normalized.get("width"), "height": normalized.get("height"),
                "n": normalized.get("batch_size"), "accounts": output,
                "source": "krea_jobs_estimate"}
    except Exception as exc:
        raise _raise(exc) from exc


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in account.items() if key != "cookies"},
            "cookies_ready": bool(account.get("cookies"))}


@app.get("/api/admin/accounts", dependencies=[Depends(admin_auth)])
def admin_accounts() -> list[dict[str, Any]]:
    return [_public_account(item) for item in store.accounts()]


@app.get("/api/admin/models", dependencies=[Depends(admin_auth)])
def admin_models() -> list[dict[str, Any]]:
    return public_models()


@app.post("/api/admin/accounts", dependencies=[Depends(admin_auth)])
@app.post("/api/accounts/sync/browser", dependencies=[Depends(sync_auth)])
def upsert_account(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        existing = next((item for item in store.accounts()
                         if item["name"] == str(body.get("name") or "").strip()), None)
        account = store.upsert_account(name=str(body.get("name") or ""),
                                       cookies=body.get("cookies"),
                                       proxy_url=str(body.get("proxy_url") or ""),
                                       user_agent=str(body.get("user_agent") or (existing["user_agent"] if existing else "")),
                                       project_id=str(body.get("project_id") or ""),
                                       max_concurrency=int(body["max_concurrency"]) if "max_concurrency" in body
                                       else existing["max_concurrency"] if existing else 1,
                                       login_status=("ready" if body.get("project_id") else "project_required")
                                       if body.get("cookies") else
                                       ("ready" if existing and existing["cookies"]
                                        and existing["login_status"] == "project_required"
                                        and body.get("project_id") else None),
                                       enabled=bool(body.get("enabled", True)))
        return _public_account(account)
    except Exception as exc:
        raise _raise(exc) from exc


@app.post("/api/accounts/batch-import", dependencies=[Depends(admin_auth)])
@app.post("/api/admin/accounts/batch-import", dependencies=[Depends(admin_auth)])
def batch_import_accounts(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    source = body.get("text")
    if not isinstance(source, str):
        raise HTTPException(status_code=422, detail="text must be a string")
    try:
        entries, errors = parse_batch(source)
        concurrency = int(body.get("max_concurrency", 1))
        if not 1 <= concurrency <= 16:
            raise ValueError("max_concurrency must be between 1 and 16")
    except ValueError as exc:
        raise _raise(exc) from exc
    parse_error_count = len(errors)
    imported = []
    started = 0
    existing_count = 0
    old_by_name = {item["name"].casefold(): item for item in store.accounts()}
    for entry in entries:
        try:
            old = old_by_name.get(entry["name"].casefold())
            if old:
                existing_count += 1
            account = store.upsert_account(
                name=old["name"] if old else entry["name"],
                password_ciphertext=encrypt_password(entry["password"]),
                proxy_url=(entry["proxy_url"] or old["proxy_url"]) if old else entry["proxy_url"],
                login_status="login_pending" if not old or not old["cookies"] else old["login_status"],
                max_concurrency=concurrency,
                enabled=True,
            )
            imported.append(_public_account(account))
            if bool(body.get("start_login", True)) and service.schedule_login(account["id"]):
                started += 1
        except Exception as exc:
            errors.append({"line": entry["line"], "message": str(exc)[:200]})
    input_count = sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in source.splitlines())
    return {"accounts": imported, "count": len(imported), "input_count": input_count,
            "duplicate_count": max(0, input_count - len(entries) - parse_error_count) + existing_count,
            "existing_count": existing_count, "login_started": bool(body.get("start_login", True)),
            "login_started_count": started, "errors": errors}


@app.post("/api/admin/accounts/{account_id}/login", dependencies=[Depends(admin_auth)])
def login_account(account_id: int) -> dict[str, Any]:
    account = store.account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    if not account["has_password"]:
        raise HTTPException(status_code=422, detail="account has no imported password")
    started = service.schedule_login(account_id)
    return {"started": started, "account": _public_account(store.account(account_id))}


@app.post("/api/admin/accounts/{account_id}/browser/open", dependencies=[Depends(admin_auth)])
def open_account_browser(account_id: int) -> JSONResponse:
    try:
        return JSONResponse(service.open_manual_browser(account_id),
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _raise(exc) from exc


@app.get("/api/admin/accounts/{account_id}/browser", dependencies=[Depends(admin_auth)])
def account_browser_snapshot(account_id: int) -> JSONResponse:
    try:
        return JSONResponse(service.manual_browser_snapshot(account_id),
                            headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise _raise(exc) from exc


@app.post("/api/admin/accounts/{account_id}/browser/action", dependencies=[Depends(admin_auth)])
def account_browser_action(account_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        return service.manual_browser_action(account_id, body)
    except Exception as exc:
        raise _raise(exc) from exc


@app.post("/api/admin/accounts/{account_id}/browser/complete", dependencies=[Depends(admin_auth)])
def complete_account_browser(account_id: int) -> dict[str, Any]:
    try:
        return _public_account(service.complete_manual_browser(account_id))
    except Exception as exc:
        raise _raise(exc) from exc


@app.post("/api/admin/accounts/{account_id}/browser/close", dependencies=[Depends(admin_auth)])
def close_account_browser(account_id: int) -> dict[str, bool]:
    service.close_manual_browser(account_id)
    return {"closed": True}


@app.patch("/api/admin/accounts/{account_id}", dependencies=[Depends(admin_auth)])
def patch_account(account_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not store.account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    allowed = {"enabled", "proxy_url", "project_id", "max_concurrency"}
    if set(body) - allowed:
        raise HTTPException(status_code=422, detail="unsupported account patch fields")
    if body.get("project_id") and store.account(account_id)["cookies"]:
        body["login_status"] = "ready"
    store.set_account(account_id, **body)
    return _public_account(store.account(account_id))


@app.post("/api/admin/accounts/{account_id}/refresh", dependencies=[Depends(admin_auth)])
def refresh_account(account_id: int) -> dict[str, Any]:
    account = store.account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="account not found")
    try:
        client = service._client(account)
        balance = client.balance()
        store.set_account(account_id, balance=balance, cookies=client.export_cookies(),
                          last_error="", login_status="ready" if account["project_id"] else "project_required")
        return _public_account(store.account(account_id))
    except Exception as exc:
        store.set_account(account_id, last_error=str(exc)[:600],
                          login_status="session_required" if isinstance(exc, KreaError)
                          and exc.status_code in {401, 403} else account["login_status"])
        raise _raise(exc) from exc


@app.get("/api/admin/tasks", dependencies=[Depends(admin_auth)])
def admin_tasks(limit: int = 100) -> list[dict[str, Any]]:
    return [service.public_task(item) for item in store.tasks(min(max(limit, 1), 500))]


@app.get("/api/admin/tasks/{task_id}", dependencies=[Depends(admin_auth)])
def admin_task_detail(task_id: str) -> dict[str, Any]:
    task = _task(task_id)
    return {**service.public_task(task), "request": task["request"],
            "normalized": task["normalized"], "upstream_request": task["upstream_request"],
            "upstream_response": task["upstream_response"],
            "upstream_job_id": task["upstream_job_id"],
            "balance_before": task["balance_before"], "balance_after": task["balance_after"],
            "created_at": task["created_at"], "updated_at": task["updated_at"],
            "caller_response": service.public_task(task)}


@app.delete("/api/admin/tasks/finished", dependencies=[Depends(admin_auth)])
def clear_finished_tasks() -> dict[str, int]:
    return {"deleted": store.clear_finished_tasks()}


@app.get("/api/admin/settings", dependencies=[Depends(admin_auth)])
def admin_settings() -> dict[str, int]:
    return {"poll_interval_seconds": service.poll_seconds,
            "task_timeout_seconds": service.timeout_seconds,
            "task_workers": service.workers}


@app.patch("/api/admin/settings", dependencies=[Depends(admin_auth)])
def patch_settings(body: dict[str, Any] = Body(...)) -> dict[str, int]:
    limits = {"poll_interval_seconds": (2, 120), "task_timeout_seconds": (60, 7200)}
    if not body or set(body) - set(limits):
        raise HTTPException(status_code=422, detail="unsupported settings")
    values: dict[str, int] = {}
    for key, value in body.items():
        if isinstance(value, bool) or not isinstance(value, int) or not limits[key][0] <= value <= limits[key][1]:
            raise HTTPException(status_code=422, detail=f"invalid {key}")
        values[key] = value
    store.set_settings(values)
    service.poll_seconds = values.get("poll_interval_seconds", service.poll_seconds)
    service.timeout_seconds = values.get("task_timeout_seconds", service.timeout_seconds)
    return admin_settings()


@app.get("/api/admin/model-costs", dependencies=[Depends(admin_auth)])
def admin_costs(limit: int = 100) -> list[dict[str, Any]]:
    return store.cost_samples(min(max(limit, 1), 500))
