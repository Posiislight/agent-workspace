import asyncio
import hashlib
import hmac as hmac_mod
import json
import uuid

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.config import Settings
from app.db import ensure_schema, get_task_by_pr
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.state import initial_state
from app.main import create_app
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services

pytestmark = pytest.mark.integration

SECRET = "whsec"
URLS = "URL: https://docs.example.com/api"
OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 1"}], "done": True})
OPS2 = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 2"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
SCRIPT_APPROVE = ["PLAN: fix add", URLS, "notes", OPS, APPROVED]
SCRIPT_REJECT = SCRIPT_APPROVE + ["REVISED PLAN", URLS, "notes2", OPS2, APPROVED]


def make_settings():
    return Settings(github_pat="p", github_webhook_secret=SECRET)


def _services(script, github, redis_client):
    return make_services(
        llm=StubLLM(list(script)),
        sandbox=StubSandbox(),
        github=github,
        publisher=EventPublisher(redis_client),
        redis=redis_client,
        settings=make_settings(),
    )


def _state(task_id, description):
    return dict(initial_state(task_id, description, "org/repo", "main", "pytest -q", {}))


async def wait_for(predicate, timeout=10.0, msg="condition never met"):
    async def poll():
        last = None
        for _ in range(300):
            last = await predicate()
            if last:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"{msg}; last={last!r}")
    return await asyncio.wait_for(poll(), timeout)


def _post_headers(body: bytes):
    return {
        "x-hub-signature-256": "sha256=" + hmac_mod.new(
            SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": "pull_request_review",
        "content-type": "application/json",
    }


def _comment_headers(body: bytes):
    return {
        "x-hub-signature-256": "sha256=" + hmac_mod.new(
            SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": "issue_comment",
        "content-type": "application/json",
    }


async def test_approve_via_webhook_reaches_done_with_ready_pr(settings, redis_client):
    await ensure_schema(settings.database_url)
    tid = f"t-rt-{uuid.uuid4().hex[:8]}"
    github = StubGitHub()
    services = _services(SCRIPT_APPROVE, github, redis_client)
    services.pg_dsn = settings.database_url
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    app = create_app(services=services, graph=graph)
    app.state.run_manager = rm

    await rm.prepare(tid, _state(tid, "add a feature"))
    await rm.start(tid, _state(tid, "add a feature"))
    st = await wait_for(awaiting(rm, tid), msg="never reached awaiting_approval")
    assert st["status"] == "awaiting_approval"
    assert len(github.created_prs) == 1
    assert "add a feature" in github.created_prs[0]["body"]
    assert github.created_prs[0]["draft"] is True

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        body = json.dumps({"action": "submitted",
                           "pull_request": {"number": 11},
                           "review": {"state": "approved", "body": ""},
                           "repository": {"full_name": "org/repo"}}).encode()
        r = await client.post("/webhooks/github", content=body,
                              headers=_post_headers(body))
        assert r.status_code == 202, r.json()
        assert r.json() == {"ok": True, "resumed": True}

    st = await wait_for(done(rm, tid), msg="never reached done")
    assert st["status"] == "done"
    assert st["pr_url"] == "https://github.com/org/repo/pull/11"
    assert github.ready == [11]
    assert len(github.comments) == 1
    summary = github.comments[0][1]
    assert "Total cost" in summary and "Retry cycles" in summary
    assert github.merged == []
    row = await get_task_by_pr(settings.database_url, "org/repo", 11)
    assert row is not None and row["task_id"] == tid

    entries = await redis_client.xrange(f"aw:{tid}:events", min="-", max="+")
    assert entries
    wake = [e for e in entries
            if e[1].get("type") == "sleep_wake"]
    assert wake, f"no sleep_wake event; got {entries}"
    data = json.loads(wake[0][1]["data"])
    assert "resume_latency_seconds" in data
    assert data["resume_latency_seconds"] >= 0.0


async def test_reject_via_webhook_loops_back_to_planner(settings, redis_client):
    await ensure_schema(settings.database_url)
    tid = f"t-rj-{uuid.uuid4().hex[:8]}"
    github = StubGitHub()
    services = _services(SCRIPT_REJECT, github, redis_client)
    services.pg_dsn = settings.database_url
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    app = create_app(services=services, graph=graph)
    app.state.run_manager = rm

    await rm.prepare(tid, _state(tid, "add a feature"))
    await rm.start(tid, _state(tid, "add a feature"))
    st = await wait_for(awaiting(rm, tid), msg="never reached awaiting_approval")
    assert st["status"] == "awaiting_approval"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        body = json.dumps({"action": "created",
                           "issue": {"number": 11, "pull_request": {"u": 1}},
                           "comment": {"body": "reject: wrong approach",
                                       "user": {"type": "User"}},
                           "repository": {"full_name": "org/repo"}}).encode()
        r = await client.post("/webhooks/github", content=body,
                              headers=_comment_headers(body))
        assert r.status_code == 202

    # The reject loops Planner -> ... -> Reviewer and pauses at the gate again; the
    # reviewer re-approves on the way, so approval_status is not a stable signal.
    # Wait for the second pause and assert the loop itself.
    async def repaused():
        st = await rm.get_state(tid)
        return st if (st and st.get("status") == "awaiting_approval"
                      and st.get("plan") == "REVISED PLAN") else None
    st = await wait_for(repaused, msg="never looped back to the gate with a revised plan")
    assert st["task_description"].endswith("HUMAN FEEDBACK: wrong approach")
    planner_calls = [c for c in services.llm.calls
                     if "PREVIOUS PLAN" in c["messages"][-1]["content"]]
    assert len(planner_calls) == 1
    assert "HUMAN FEEDBACK: wrong approach" in planner_calls[0]["messages"][-1]["content"]


def awaiting(rm, tid):
    async def get():
        st = await rm.get_state(tid)
        return st if st and st.get("status") == "awaiting_approval" else None
    return get


def done(rm, tid):
    async def get():
        st = await rm.get_state(tid)
        return st if st and st.get("status") == "done" else None
    return get


def matching(rm, tid, marker):
    async def get():
        st = await rm.get_state(tid)
        if st and marker in (st.get("task_description") or ""):
            return st
        return None
    return get
