import json

import httpx

from app.github.client import GitHubClient


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_find_open_pr_matches_head():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/repos/org/repo/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["head"] == "org:feature"
        assert request.headers["Authorization"] == "Bearer tok123"
        return httpx.Response(200, json=[{"number": 7, "html_url": "u", "draft": True}])

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(transport=_transport(handler),
                                   base_url="https://api.github.com")
    try:
        pr = await gh.find_open_pr("org/repo", "org:feature")
    finally:
        await gh.aclose()
    assert pr == {"number": 7, "html_url": "u", "draft": True}
    assert calls[0].startswith("https://api.github.com/repos/org/repo/pulls")


async def test_find_open_pr_none_when_empty():
    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.github.com")
    try:
        assert await gh.find_open_pr("org/repo", "org:feature") is None
    finally:
        await gh.aclose()


async def test_create_pr_sends_draft_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(201, json={"number": 3, "html_url": "url", "draft": True})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        pr = await gh.create_pr("org/repo", title="T", body="B",
                                head="org:aw/x", base="main", draft=True)
    finally:
        await gh.aclose()
    assert pr["number"] == 3 and pr["draft"] is True
    assert seen == {"title": "T", "body": "B", "head": "aw/x", "base": "main",
                    "draft": True}
