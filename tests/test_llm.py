import httpx
import pytest

from app.llm.backoff import is_transient, with_retry
from app.llm.openrouter import OpenRouterClient
from app.llm.pricing import PriceTable


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://openrouter.ai/api/v1")


async def test_chat_extracts_usage():
    def handler(request):
        assert request.url.path == "/api/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "the plan"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    c = OpenRouterClient(api_key="k", client=mock_client(handler))
    res = await c.chat("m", [{"role": "user", "content": "hi"}])
    assert res.text == "the plan"
    assert res.prompt_tokens == 100 and res.completion_tokens == 20


async def test_chat_streams_deltas_and_final_usage():
    sse_lines = [
        'data: {"choices": [{"delta": {"content": "hel"}}]}',
        'data: {"choices": [{"delta": {"content": "lo"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}',
        "data: [DONE]",
    ]

    def handler(request):
        return httpx.Response(200, content="\n\n".join(sse_lines).encode())

    c = OpenRouterClient(api_key="k", client=mock_client(handler))
    seen = []
    res = await c.chat("m", [{"role": "user", "content": "hi"}], stream_cb=seen.append)
    assert res.text == "hello"
    assert "".join(seen) == "hello"
    assert res.prompt_tokens == 7 and res.completion_tokens == 2


async def test_price_table_cost_math():
    pt = PriceTable({"openai/gpt-4.1-mini": {"prompt": 0.000003, "completion": 0.000015}})
    assert abs(pt.cost("openai/gpt-4.1-mini", 1000, 100) - 0.0045) < 1e-12
    assert pt.cost("unknown/model", 100, 100) == 0.0


async def test_price_table_fetch_parses_strings():
    def handler(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "x/y", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
            {"id": "z/bad", "pricing": {"prompt": "n/a", "completion": "0"}},
        ]})

    pt = await PriceTable.fetch(mock_client(handler))
    assert abs(pt.cost("x/y", 1_000_000, 0) - 3.0) < 1e-9
    assert pt.cost("z/bad", 100, 100) == 0.0


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, s):
        self.delays.append(s)


async def test_backoff_retries_transient_then_succeeds():
    import httpx

    from app.llm.backoff import with_retry
    calls = {"n": 0}
    sleeper = FakeSleep()

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPStatusError("boom", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(429))
        return "ok"

    assert await with_retry(flaky, attempts=3, base_delay=1.0, sleep=sleeper) == "ok"
    assert calls["n"] == 3
    assert sleeper.delays == [1.0, 4.0]


async def test_backoff_does_not_retry_client_errors():
    import httpx

    from app.llm.backoff import with_retry
    calls = {"n": 0}

    async def bad_request():
        calls["n"] += 1
        raise httpx.HTTPStatusError("nope", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(400))

    with pytest.raises(httpx.HTTPStatusError):
        await with_retry(bad_request, attempts=3, base_delay=0.0, sleep=FakeSleep())
    assert calls["n"] == 1


def test_transient_classification():
    from app.llm.backoff import is_transient

    t = httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"),
                              response=httpx.Response(503))
    assert is_transient(t)
    bad = httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"),
                                response=httpx.Response(400))
    assert not is_transient(bad)
    assert is_transient(httpx.ConnectTimeout("t"))
