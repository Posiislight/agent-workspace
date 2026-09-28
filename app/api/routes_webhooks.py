from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import get_task_by_pr
from app.github.webhooks import classify_event, classify_followup, verify_signature
from app.services.run_manager import (
    AlreadyRunning,
    FollowupNotAllowed,
    NotAwaitingApproval,
    TaskNotFound,
)

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
    if not isinstance(payload, dict):
        return _err(400, "invalid json")
    event = request.headers.get("x-github-event", "")
    follow = classify_followup(event, payload)
    if follow:
        return await _followup(request, payload, follow)
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


async def _followup(request: Request, payload: dict, follow: dict):
    services = request.app.state.services
    rm = request.app.state.run_manager
    extra = None
    if follow["kind"] == "comment":
        task = await get_task_by_pr(services.pg_dsn, _repo(payload), follow["pr_number"])
        if not task:
            return _err(404, "no task for PR")
        task_id, instruction = task["task_id"], follow["instruction"]
    else:
        task_id = follow["task_id"]
        state = await rm.get_state(task_id)
        if not state:
            return _err(404, "no task for branch")
        key = f"{follow['sha']}:{follow['name']}"
        seen = list(state.get("ci_seen") or [])
        attempts = int(state.get("ci_fix_attempts") or 0)
        if key in seen:
            return JSONResponse({"ok": True, "followup": False, "reason": "already handled"})
        if attempts >= services.settings.ci_autofix_max_attempts:
            return JSONResponse({"ok": True, "followup": False,
                                 "reason": "ci autofix attempt cap reached"})
        instruction = await _ci_instruction(services, _repo(payload), follow)
        extra = {"ci_seen": seen + [key], "ci_fix_attempts": attempts + 1}
    try:
        await rm.followup(task_id, instruction, source=follow["source"], extra=extra)
    except TaskNotFound:
        return _err(404, "task not found")
    except AlreadyRunning:
        return _err(409, "task already running")
    except FollowupNotAllowed as e:
        return _err(409, str(e))
    return {"ok": True, "followup": True, "task_id": task_id}


async def _ci_instruction(services, repo: str, follow: dict) -> str:
    log = follow.get("summary") or ""
    gh = services.github
    if follow.get("app") == "github-actions" and follow.get("job_id") and gh is not None \
            and hasattr(gh, "job_log_tail"):
        try:
            log = await gh.job_log_tail(repo, follow["job_id"])
        except Exception as e:  # noqa: BLE001 - fall back to the check summary
            log = f"{log}\n(could not fetch job log: {e!r})"
    return (f"CI check '{follow['name']}' failed on {follow['sha'][:7]}. Find the root cause "
            "and fix it in the code this task changed. Do not skip, delete or weaken tests."
            f"\n\nCI log tail:\n{log[-6000:]}")


def _repo(payload):
    return ((payload.get("repository") or {}).get("full_name")) or ""


def _err(code, detail):
    return JSONResponse({"ok": False, "detail": detail}, status_code=code)
