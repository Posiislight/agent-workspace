import base64
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.db import list_tasks
from app.events.sse import sse_events
from app.graph.state import initial_state
from app.harness import HARNESSES
from app.services.run_manager import (
    AlreadyRunning,
    FollowupNotAllowed,
    NotAwaitingApproval,
    TaskNotFound,
)

router = APIRouter(prefix="/tasks")

MAX_CANDIDATES = 4


class TaskCreate(BaseModel):
    task_description: str
    repo: str
    base_branch: str = "main"
    test_command: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)
    harness: str | None = None
    candidates: list[str] = Field(default_factory=list)
    budget_usd: float | None = None
    preview_command: str | None = None
    preview_port: int | None = None
    preview_path: str | None = None
    preview_setup: str | None = None


class FeedbackBody(BaseModel):
    feedback: str = ""


class FollowupBody(BaseModel):
    message: str


class BudgetBody(BaseModel):
    budget_usd: float


@router.post("", status_code=201)
async def create_task(body: TaskCreate, request: Request):
    if "/" not in body.repo:
        raise HTTPException(422, "repo must look like org/name")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", body.repo):
        raise HTTPException(422, "repo must match org/name")
    if not re.fullmatch(r"[\w./-]+", body.base_branch):
        raise HTTPException(422, "invalid base_branch")
    settings = request.app.state.services.settings
    harness = body.harness or settings.default_harness
    for h in [harness, *body.candidates]:
        if h not in HARNESSES:
            raise HTTPException(422, f"unknown harness {h!r}; expected one of {HARNESSES}")
    if len(body.candidates) > MAX_CANDIDATES:
        raise HTTPException(422, f"at most {MAX_CANDIDATES} candidates")
    budget = settings.default_budget_usd if body.budget_usd is None else body.budget_usd
    if budget < 0:
        raise HTTPException(422, "budget_usd must be >= 0")
    preview = None
    if body.preview_command:
        preview = {"command": body.preview_command, "port": body.preview_port or 8000,
                   "path": body.preview_path or "/", "setup": body.preview_setup}
    task_id = uuid.uuid4().hex
    st = dict(initial_state(task_id, body.task_description, body.repo, body.base_branch,
                            body.test_command or "", body.model_overrides, harness=harness,
                            candidates=body.candidates, budget_usd=budget, preview=preview))
    rm = request.app.state.run_manager
    await rm.prepare(task_id, st)
    await rm.start(task_id, st)
    return {"task_id": task_id}


@router.get("")
async def list_all(request: Request, limit: int = 100):
    dsn = request.app.state.services.pg_dsn
    rows = await list_tasks(dsn, limit=max(1, min(limit, 500)))
    for r in rows:
        for k in ("created_at", "updated_at"):
            if r.get(k) is not None:
                r[k] = r[k].isoformat()
    return {"tasks": rows}


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request):
    st = await request.app.state.run_manager.get_state(task_id)
    if not st:
        raise HTTPException(404, "task not found")
    return st


async def _resume(request: Request, task_id: str, decision: str, feedback: str):
    try:
        await request.app.state.run_manager.resume(task_id, decision, feedback)
    except TaskNotFound as e:
        raise HTTPException(404, str(e)) from e
    except NotAwaitingApproval as e:
        raise HTTPException(409, str(e)) from e
    return {"resumed": True, "decision": decision}


@router.post("/{task_id}/approve", status_code=202)
async def approve(task_id: str, request: Request, body: FeedbackBody | None = None):
    return await _resume(request, task_id, "approved", (body.feedback if body else "") or "")


@router.post("/{task_id}/reject", status_code=202)
async def reject(task_id: str, request: Request, body: FeedbackBody | None = None):
    return await _resume(request, task_id, "rejected", (body.feedback if body else "") or "")


@router.post("/{task_id}/followups", status_code=202)
async def followup(task_id: str, body: FollowupBody, request: Request):
    """Keep chatting after the PR: wakes the same agent on the same branch."""
    if not body.message.strip():
        raise HTTPException(422, "message is empty")
    try:
        await request.app.state.run_manager.followup(task_id, body.message.strip(), "chat")
    except TaskNotFound as e:
        raise HTTPException(404, str(e)) from e
    except AlreadyRunning as e:
        raise HTTPException(409, "task is running; wait for it to pause") from e
    except FollowupNotAllowed as e:
        raise HTTPException(409, str(e)) from e
    return {"accepted": True}


@router.post("/{task_id}/budget", status_code=202)
async def raise_budget(task_id: str, body: BudgetBody, request: Request):
    if body.budget_usd <= 0:
        raise HTTPException(422, "budget_usd must be > 0 (use /reject to stop)")
    try:
        await request.app.state.run_manager.raise_budget(task_id, body.budget_usd)
    except TaskNotFound as e:
        raise HTTPException(404, str(e)) from e
    except (NotAwaitingApproval, AlreadyRunning) as e:
        raise HTTPException(409, "task is not waiting on its budget") from e
    return {"resumed": True, "budget_usd": body.budget_usd}


@router.get("/{task_id}/proof/{name}.png")
async def proof_image(task_id: str, name: str, request: Request):
    redis = request.app.state.services.redis
    if name not in ("before", "after") or redis is None:
        raise HTTPException(404, "no such image")
    b64 = await redis.get(f"aw:{task_id}:proof:{name}")
    if not b64:
        raise HTTPException(404, "no such image")
    return Response(base64.b64decode(b64), media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


@router.get("/{task_id}/artifacts")
async def artifacts(task_id: str, request: Request):
    return await request.app.state.run_manager.artifacts(task_id)


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request):
    return StreamingResponse(
        sse_events(request.app.state.services.redis, task_id),
        media_type="text/event-stream")