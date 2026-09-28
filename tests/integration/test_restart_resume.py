import asyncio

import httpx
import pytest

from app.db import ensure_schema, make_checkpointer
from app.graph.build import build_graph
from app.main import create_app
from tests.fakes import FakePublisher, StubAgent, StubGitHub, make_services

pytestmark = pytest.mark.integration

SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com", "notes",
          "DONE",
          '{"verdict": "approved", "comments": []}']


async def wait_status(client, task_id, wanted, timeout=10.0):
    async def poll():
        last = None
        for _ in range(300):
            r = await client.get(f"/tasks/{task_id}")
            last = r.json()
            if last.get("status") == wanted:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"status never reached {wanted}; last={last}")
    return await asyncio.wait_for(poll(), timeout)


async def test_interrupt_survives_full_process_restart(settings, redis_client):
    await ensure_schema(settings.database_url)

    # --- process A: run to the approval interrupt, then "die" ---
    services_a = make_services(github=StubGitHub(), agent=StubAgent(SCRIPT), publisher=FakePublisher(),
                               redis=redis_client, pg_dsn=settings.database_url)
    cm_a = make_checkpointer(settings.database_url)
    checkpointer_a = await cm_a.__aenter__()
    await checkpointer_a.setup()
    app_a = create_app(services=services_a, graph=build_graph(services_a, checkpointer_a))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo", "template_id": "codex"})
        tid = r.json()["task_id"]
        st = await wait_status(client, tid, "awaiting_approval")
        assert st["paused_at"]
    await cm_a.__aexit__(None, None, None)  # process A exits

    # --- process B: brand-new graph + checkpointer over the SAME Postgres ---
    services_b = make_services(github=StubGitHub(), publisher=FakePublisher(), redis=redis_client,
                               pg_dsn=settings.database_url)  # no LLM needed post-approval
    cm_b = make_checkpointer(settings.database_url)
    checkpointer_b = await cm_b.__aenter__()
    await checkpointer_b.setup()  # idempotent
    app_b = create_app(services=services_b, graph=build_graph(services_b, checkpointer_b))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b),
                                 base_url="http://t") as client_b:
        r = await client_b.post(f"/tasks/{tid}/approve", json={})
        assert r.status_code == 202
        st = await wait_status(client_b, tid, "done")
        assert st["resumed_at"] and st["paused_at"]
    await cm_b.__aexit__(None, None, None)