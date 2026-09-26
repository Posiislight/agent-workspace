from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import get_task_by_pr
from app.github.webhooks import classify_event, verify_signature
from app.services.run_manager import AlreadyRunning, NotAwaitingApproval, TaskNotFound

router = APIRouter()


@router.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request):
    settings = request.app.state.services.settings
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    if not verify_signature(settings.github_webhook_secret, body, sig):
        return _err(400, "invalid signature")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err(400, "invalid json")
    event = request.headers.get("x-github-event", "")
    decision = classify_event(event, payload)
    if not decision:
        return JSONResponse({"ok": True, "resumed": False})
    task = await get_task_by_pr(request.app.state.services.pg_dsn,
                                   _repo(payload), decision["pr_number"])
    if not task:
        return _err(404, "no task for PR")
    try:
        await request.app.state.run_manager.resume(
            task["task_id"], decision["decision"], decision["feedback"])
    except TaskNotFound:
        return _err(404, "task not found")
    except AlreadyRunning:
        return _err(409, "task already running")
    except NotAwaitingApproval:
        return _err(409, "task not awaiting approval")
    return {"ok": True, "resumed": True}


def _repo(payload):
    return ((payload.get("repository") or {}).get("full_name")) or ""


def _err(code, detail):
    return JSONResponse({"ok": False, "detail": detail}, status_code=code)
