import json
import httpx
import pytest
from app.sandbox.maritime_agent import AgentTimeout, MaritimeAgentClient


def make_client(handler) -> MaritimeAgentClient:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.maritime.sh", transport=transport)
    return MaritimeAgentClient("k", client=client)


async def test_create_posts_template_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents"
        body = json.loads(request.content)
        assert body["templateId"] == "codex"
        return httpx.Response(201, json={"id": "a-1"})
    c = make_client(handler)
    assert await c.create("aw-task-x", "codex") == "a-1"


async def test_wait_active_polls_until_active():
    states = ["deploying", "deploying", "active"]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/a-1"
        return httpx.Response(200, json={"status": states.pop(0)})
    c = make_client(handler)
    assert await c.wait_active("a-1", timeout_s=30, poll_s=0) == "a-1"


async def test_wait_active_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "deploying"})
    c = make_client(handler)
    with pytest.raises(AgentTimeout):
        await c.wait_active("a-1", timeout_s=0.2, poll_s=0.05)


async def test_chat_returns_response_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/a-1/chat"
        body = json.loads(request.content)
        assert body["message"] == "hi"
        assert body["conversation_id"] == "conv-1"
        return httpx.Response(200, json={"response": "PONG"})
    c = make_client(handler)
    assert await c.chat("a-1", "hi", "conv-1") == "PONG"


async def test_chat_retries_transient_errors():
    calls = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"response": "ok"})
    c = make_client(handler)
    c._retry_base = 0
    assert await c.chat("a-1", "m", "c") == "ok"
    assert calls["n"] == 3


async def test_sleep_and_compute_seconds():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sleep"):
            return httpx.Response(200, json={})
        assert request.url.path == "/api/agents/a-1"
        return httpx.Response(200, json={"status": "sleeping", "totalComputeSeconds": 125})
    c = make_client(handler)
    await c.sleep("a-1")
    assert await c.total_compute_seconds("a-1") == 125.0
