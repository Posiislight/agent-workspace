import asyncio
import hashlib
import hmac as hmac_mod
import json
import uuid

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.config import Settings
from app.db import ensure_schema, get_task
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.state import initial_state
from app.main import create_app
from app.services.run_manager import RunManager
from tests.fakes import StubAgent, StubGitHub, StubSandbox, make_services

pytestmark = pytest.mark.integration

SECRET = "whsec"
APPROVED = json.dumps({"verdict": "approved", "comments": []})
SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com/api", "notes",
          "DONE", APPROVED]


def _state(tid):
    return dict(initial_state(tid, "add a feature", "org/repo", "main",
                              "pytest -q"))


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


async def test_cost_totals_reach_redis_pr_and_audit_row(settings, redis_client):
    await ensure_schema(settings.database_url)
    tid = f"t-cost-{uuid.uuid4().hex[:8]}"
    github = StubGitHub()
    services = make_services(
        agent=StubAgent(list(SCRIPT)), sandbox=StubSandbox(), github=github,
        publisher=EventPublisher(redis_client), redis=redis_client,
        settings=Settings(github_pat="p", github_webhook_secret=SECRET,
                          vm_cost_per_hour=6.0))
    services.pg_dsn = settings.database_url
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    app = create_app(services=services, graph=graph)
    app.state.run_manager = rm

    await rm.prepare(tid, _state(tid))
    await rm.start(tid, _state(tid))
    st = await wait_for(lambda: _status(rm, tid, "awaiting_approval")(),
                        msg="never reached awaiting_approval")

    snap = await services.cost.snapshot(tid)
    # LLM cost events are gone (apply_llm_cost removed in Task 5); only VM
    # awake-minutes are tracked now.
    assert snap["llm_cost"] == 0.0
    assert snap["vm_minutes"] > 0 and snap["vm_cost"] > 0
    live_minutes = await redis_client.get(f"aw:{tid}:vm_minutes")
    assert float(live_minutes) == snap["vm_minutes"]
    assert st["vm_cost"] == snap["vm_cost"]

    body = json.dumps({"action": "submitted", "pull_request": {"number": 11},
                       "review": {"state": "approved", "body": ""},
                       "repository": {"full_name": "org/repo"}}).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac_mod.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": "pull_request_review",
        "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/webhooks/github", content=body, headers=headers)
        assert r.status_code == 202, r.json()

    st = await wait_for(lambda: _status(rm, tid, "done")(),
                        msg="never reached done")
    assert github.updated_bodies and github.updated_bodies[0][0] == 11
    assert "## Final totals" in github.updated_bodies[0][1]
    summary = github.comments[0][1]
    assert "awake min" in summary

    row = await get_task(settings.database_url, tid)
    assert row["vm_cost"] > 0 and row["vm_minutes"] > 0

    events = await redis_client.xrange(f"aw:{tid}:events", min="-", max="+")
    cost_events = [e for e in events if e[1].get("type") == "cost_update"]
    # apply_llm_cost is gone (Task 5): VM cost arrives via the RunManager
    # mirror, not node-emitted cost_update events.
    assert not cost_events


def _status(rm, tid, wanted):
    async def get():
        s = await rm.get_state(tid)
        return s if s and s.get("status") == wanted else None
    return get
