import json
import httpx
import pytest
from app.sandbox.maritime_agent import AgentRateLimited, AgentTimeout, MaritimeAgentClient


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


ACK = ("I'm on it. This needs more than a few seconds; message me again shortly "
       "and I'll have your answer ready.")
BUSY = "Still working on your last request. Message me again in a moment and I'll have the answer."


async def test_chat_polls_through_ack_and_busy_until_real_answer():
    replies = [ACK, BUSY, BUSY, "REAL ANSWER"]
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["message"], body["conversation_id"]))
        return httpx.Response(200, json={"response": replies.pop(0)})
    c = make_client(handler)
    c._poll_s = 0
    assert await c.chat("a-1", "do the work", "conv-9") == "REAL ANSWER"
    assert seen[0] == ("do the work", "conv-9")
    # follow-ups nudge the SAME conversation, never resend the task
    assert all(cid == "conv-9" and msg != "do the work" for msg, cid in seen[1:])
    assert len(seen) == 4


async def test_chat_gives_up_when_agent_stays_busy():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": BUSY})
    c = make_client(handler)
    c._poll_s = 0.01
    with pytest.raises(AgentTimeout):
        await c.chat("a-1", "m", "c", timeout_s=0.1)


async def test_chat_raises_on_error_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": None, "error": "harness crashed"})
    c = make_client(handler)
    with pytest.raises(RuntimeError, match="harness crashed"):
        await c.chat("a-1", "m", "c")


RATE_LIMITED = ("The model provider is rate limiting this agent right now. Try again in a minute. "
                "Details: exceeded retry limit, last status: 429 Too Many Requests, request id: x")


async def test_chat_waits_out_rate_limit_and_resends_same_message():
    replies = [RATE_LIMITED, RATE_LIMITED, '{"verdict": "approved"}']
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((body["message"], body["conversation_id"]))
        return httpx.Response(200, json={"response": replies.pop(0)})
    c = make_client(handler)
    c._rate_limit_wait_s = 0
    assert await c.chat("a-1", "review this", "conv-r") == '{"verdict": "approved"}'
    # the model never answered, so the ORIGINAL request is resent, not a nudge
    assert seen == [("review this", "conv-r")] * 3


async def test_chat_rate_limit_while_polling_resends_the_nudge():
    replies = [ACK, RATE_LIMITED, "DONE"]
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["message"])
        return httpx.Response(200, json={"response": replies.pop(0)})
    c = make_client(handler)
    c._poll_s = 0
    c._rate_limit_wait_s = 0
    assert await c.chat("a-1", "work", "c") == "DONE"
    assert seen[0] == "work" and seen[1] == seen[2] != "work"


async def test_chat_raises_instead_of_returning_rate_limit_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": RATE_LIMITED})
    c = make_client(handler)
    c._rate_limit_wait_s = 0
    with pytest.raises(AgentRateLimited):
        await c.chat("a-1", "m", "c")


def test_default_poll_interval_is_30s():
    # Each poll is a nudge message to the harness; keep them infrequent.
    assert MaritimeAgentClient("k")._poll_s == 30.0


async def test_rate_limit_error_points_at_ai_credits():
    # Maritime reports exhausted AI credits as this same "rate limiting" text,
    # so a persistent one must name credits, not just say "try again".
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": RATE_LIMITED})
    c = make_client(handler)
    c._rate_limit_wait_s = 0
    with pytest.raises(AgentRateLimited, match="AI credits"):
        await c.chat("a-1", "m", "c")


async def test_delete_removes_agent_and_tolerates_already_gone():
    seen = []
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(204 if len(seen) == 1 else 404)
    c = make_client(handler)
    await c.delete("a-1")
    await c.delete("a-1")
    assert seen == [("DELETE", "/api/agents/a-1")] * 2


async def test_exists_is_false_only_on_404():
    codes = {"a-1": 200, "a-2": 404, "a-3": 500}
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(codes[request.url.path.rsplit("/", 1)[1]],
                              json={"status": "sleeping"})
    c = make_client(handler)
    assert await c.exists("a-1") is True
    assert await c.exists("a-2") is False
    with pytest.raises(httpx.HTTPStatusError):
        await c.exists("a-3")
