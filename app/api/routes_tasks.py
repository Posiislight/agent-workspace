import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.db import list_tasks
from app.events.sse import sse_events
from app.graph.state import initial_state
from app.services.run_manager import (
    AlreadyRunning,
    NotAwaitingApproval,
    NotRestartable,
    TaskNotFound,
)

router = APIRouter(prefix="/tasks")


class TaskCreate(BaseModel):
    task_description: str
    repo: str
    base_branch: str = "main"
    test_command: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)


class FeedbackBody(BaseModel):
    feedback: str = ""


@router.post("", status_code=201)
async def create_task(body: TaskCreate, request: Request):
    if "/" not in body.repo:
        raise HTTPException(422, "repo must look like org/name")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", body.repo):
        raise HTTPException(422, "repo must match org/name")
    if not re.fullmatch(r"[\w./-]+", body.base_branch):
        raise HTTPException(422, "invalid base_branch")
    task_id = uuid.uuid4().hex
    st = dict(initial_state(task_id, body.task_description, body.repo, body.base_branch,
                            body.test_command or "", body.model_overrides))
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


@router.post("/{task_id}/restart", status_code=202)
async def restart(task_id: str, request: Request):
    try:
        await request.app.state.run_manager.restart(task_id)
    except TaskNotFound as e:
        raise HTTPException(404, str(e)) from e
    except (NotRestartable, NotAwaitingApproval, AlreadyRunning) as e:
        raise HTTPException(409, str(e)) from e
    return {"restarted": True}


@router.get("/{task_id}/artifacts")
async def artifacts(task_id: str, request: Request):
    return await request.app.state.run_manager.artifacts(task_id)


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request):
    return StreamingResponse(
        sse_events(request.app.state.services.redis, task_id),
        media_type="text/event-stream")