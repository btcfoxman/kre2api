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

from .catalog import MODELS, normalize_request, public_models
from .client import KreaClient, KreaError
from .service import Service
from .store import Store


API_KEY = os.getenv("KR_API_KEY", "")
ADMIN_TOKEN = os.getenv("KR_ADMIN_TOKEN", "")
SYNC_TOKEN = os.getenv("KR_SYNC_TOKEN", "")
if not API_KEY or not ADMIN_TOKEN:
    raise RuntimeError("KR_API_KEY and KR_ADMIN_TOKEN must be configured")

DATA_DIR = Path(os.getenv("KR_DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
store = Store(os.getenv("KR_DATABASE_PATH", str(DATA_DIR / "kre2api.db")))
service = Service(store, workers=int(os.getenv("KR_TASK_WORKERS", "4")),
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
        return HTTPException(status_code=exc.status_code, detail=str(exc))
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
        raise _raise(exc) from exc
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
    return {"status": "ok", "service": "kre2api", "models": len(MODELS)}


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


@app.post("/api/admin/videos", dependencies=[Depends(admin_auth)])
def admin_create_video(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return _create(body)


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
    return {"id": task["id"], "object": "response", "model": task["model"],
            "status": "completed" if task["status"] == "succeeded" else task["status"],
            "output": [{"type": "video_generation_call", "video_url": item["url"]}
                       for item in task["data"]], "error": task["error"]}


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
            if body.get("account_id") and account["id"] != int(body["account_id"]):
                continue
            try:
                client = service._client(account)
                balance = client.balance()
                cost = client.estimate(normalized, video_seconds=video_seconds)
                store.set_account(account["id"], balance=balance,
                                  cookies=client.export_cookies(), last_error="")
                output.append({"account_id": account["id"], "name": account["name"],
                               "balance": balance, "estimated_cost": cost,
                               "available_after": balance - cost, "eligible": balance >= cost})
            except Exception as exc:
                output.append({"account_id": account["id"], "name": account["name"],
                               "error": str(exc)[:300], "eligible": False})
        return {"model": normalized["model"], "duration": normalized["duration"],
                "resolution": normalized["resolution"], "accounts": output,
                "source": "krea_jobs_estimate"}
    except Exception as exc:
        raise _raise(exc) from exc


def _public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in account.items() if key != "cookies"}


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
        account = store.upsert_account(name=str(body.get("name") or ""),
                                       cookies=body.get("cookies") or [],
                                       proxy_url=str(body.get("proxy_url") or ""),
                                       user_agent=str(body.get("user_agent") or ""),
                                       project_id=str(body.get("project_id") or ""),
                                       enabled=bool(body.get("enabled", True)))
        return _public_account(account)
    except Exception as exc:
        raise _raise(exc) from exc


@app.patch("/api/admin/accounts/{account_id}", dependencies=[Depends(admin_auth)])
def patch_account(account_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    if not store.account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    allowed = {"enabled", "proxy_url", "project_id"}
    if set(body) - allowed:
        raise HTTPException(status_code=422, detail="unsupported account patch fields")
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
                          last_error="")
        return _public_account(store.account(account_id))
    except Exception as exc:
        store.set_account(account_id, last_error=str(exc)[:600])
        raise _raise(exc) from exc


@app.get("/api/admin/tasks", dependencies=[Depends(admin_auth)])
def admin_tasks(limit: int = 100) -> list[dict[str, Any]]:
    return [service.public_task(item) for item in store.tasks(min(max(limit, 1), 500))]


@app.get("/api/admin/model-costs", dependencies=[Depends(admin_auth)])
def admin_costs(limit: int = 100) -> list[dict[str, Any]]:
    return store.cost_samples(min(max(limit, 1), 500))
