import json

import httpx
import pytest

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


async def test_merge_pr_already_merged_returns_finalized():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        return httpx.Response(405, json={"message": "Pull Request is not mergeable"})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        result = await gh.merge_pr("org/repo", 7)
    finally:
        await gh.aclose()
    assert result == {"already_finalized": True}


async def test_mark_ready_uses_graphql_mutation():
    # REST PATCH {"draft": false} is silently ignored by GitHub; leaving draft
    # needs the markPullRequestReadyForReview GraphQL mutation.
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "GET":
            return httpx.Response(200, json={"node_id": "PR_abc", "draft": True,
                                             "state": "open"})
        return httpx.Response(200, json={"data": {"markPullRequestReadyForReview": {
            "pullRequest": {"isDraft": False}}}})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        await gh.mark_ready("org/repo", 7)
    finally:
        await gh.aclose()
    assert calls[0][:2] == ("GET", "/repos/org/repo/pulls/7")
    assert calls[1][:2] == ("POST", "/graphql")
    body = json.loads(calls[1][2])
    assert "markPullRequestReadyForReview" in body["query"]
    assert body["variables"] == {"id": "PR_abc"}


async def test_mark_ready_graphql_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"node_id": "PR_abc", "draft": True,
                                             "state": "open"})
        return httpx.Response(200, json={"errors": [{"message": "nope"}]})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        with pytest.raises(RuntimeError, match="nope"):
            await gh.mark_ready("org/repo", 7)
    finally:
        await gh.aclose()


async def test_mark_ready_on_closed_or_ready_pr_returns_finalized():
    for pr in ({"node_id": "x", "draft": True, "state": "closed"},
               {"node_id": "x", "draft": False, "state": "open"}):
        def handler(request: httpx.Request, pr=pr) -> httpx.Response:
            assert request.method == "GET"
            return httpx.Response(200, json=pr)

        gh = GitHubClient("tok123")
        gh._client = httpx.AsyncClient(
            transport=_transport(handler), base_url="https://api.github.com")
        try:
            result = await gh.mark_ready("org/repo", 7)
        finally:
            await gh.aclose()
        assert result == {"already_finalized": True}


async def test_merge_pr_other_status_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "conflict"})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        try:
            await gh.merge_pr("org/repo", 7)
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code == 409
        else:
            raise AssertionError("expected HTTPStatusError")
    finally:
        await gh.aclose()
