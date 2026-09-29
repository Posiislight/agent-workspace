import asyncio
import contextlib

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.main import create_app
from tests.fakes import StubAgent, StubGitHub, make_services

pytestmark = pytest.mark.integration

APPROVED = '{"verdict": "approved", "comments": []}'
SCRIPT_APPROVE = ["PLAN: fix add\nDONE", APPROVED]
SCRIPT_REJECT = SCRIPT_APPROVE + ["REVISED PLAN\nDONE", APPROVED]


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


async def wait_status_matching(client, task_id, wanted, predicate, timeout=15.0):
    async def poll():
        last = None
        for _ in range(300):
            r = await client.get(f"/tasks/{task_id}")
            last = r.json()
            if last.get("status") == wanted and predicate(last):
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"status never reached {wanted} (predicate); last={last}")
    return await asyncio.wait_for(poll(), timeout)


def make_app(redis_client, script):
    services = make_services(github=StubGitHub(), agent=StubAgent(list(script)),
                             publisher=EventPublisher(redis_client),
                             redis=redis_client)
    graph = build_graph(services, InMemorySaver())
    return create_app(services=services, graph=graph), services


async def assert_sse_starts(app, path, timeout=5.0):
    """httpx's ASGI transport buffers the whole response body, so an infinite SSE
    stream can never complete a request. Call the ASGI app directly and cancel
    once the response start message (status + content-type) is observed."""
    received: list[dict] = []
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        received.append(message)

    task = asyncio.create_task(app(
        {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
         "method": "GET", "path": path, "raw_path": path.encode(), "headers": [],
         "query_string": b"", "scheme": "http", "server": ("t", 80),
         "client": ("t", 123), "root_path": ""},
        receive, send))
    try:
        start = await asyncio.wait_for(_wait_response_start(received), timeout)
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
    headers = dict(start["headers"])
    assert start["status"] == 200
    assert headers[b"content-type"].decode().startswith("text/event-stream")


async def _wait_response_start(received):
    async def poll():
        for _ in range(300):
            if received and received[0]["type"] == "http.response.start":
                return received[0]
            await asyncio.sleep(0.01)
        raise AssertionError("SSE response never started")
    return await poll()


async def test_submit_watch_approve(redis_client):
    app, _services = make_app(redis_client, SCRIPT_APPROVE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo", "template_id": "codex"})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        st = await wait_status(client, tid, "awaiting_approval")
        assert st["code_diff"].startswith("diff --git")
        await assert_sse_starts(app, f"/tasks/{tid}/events")
        r = await client.post(f"/tasks/{tid}/approve", json={})
        assert r.status_code == 202
        st = await wait_status(client, tid, "done")
        art = (await client.get(f"/tasks/{tid}/artifacts")).json()
        assert art["plan"] == "PLAN: fix add" and art["code_diff"]


async def test_reject_loops_back_to_coding(redis_client):
    app, _services = make_app(redis_client, SCRIPT_REJECT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo", "template_id": "codex"})
        tid = r.json()["task_id"]
        await wait_status(client, tid, "awaiting_approval")
        r = await client.post(f"/tasks/{tid}/reject", json={"feedback": "use uuid"})
        assert r.status_code == 202
        # The first pause also reports awaiting_approval, and with the scripted LLM the
        # intermediate statuses are transient (the whole re-run takes ~60ms), so wait
        # for the second pause's feedback marker rather than a transient status.
        st = await wait_status_matching(
            client, tid, "awaiting_approval",
            lambda s: s.get("task_description", "").endswith("HUMAN FEEDBACK: use uuid"))
        assert st["task_description"].endswith("HUMAN FEEDBACK: use uuid")
        await client.post(f"/tasks/{tid}/approve", json={})
        await wait_status(client, tid, "done")


async def test_approve_missing_task_returns_404(redis_client):
    app, _services = make_app(redis_client, SCRIPT_APPROVE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks/nonexistent/approve", json={})
        assert r.status_code == 404