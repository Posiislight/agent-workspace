import hashlib
import hmac as hmac_mod
import json

import httpx
import pytest

import app.api.routes_webhooks as rw
from app.main import create_app
from app.services.run_manager import NotAwaitingApproval
from tests.fakes import FakeRedis, make_services

SECRET = "whsec"


def _post(client, event, payload, *, secret=SECRET):
    body = json.dumps(payload).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac_mod.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": event,
        "content-type": "application/json"}
    if secret != SECRET:
        headers["x-hub-signature-256"] = "sha256=" + hmac_mod.new(
            secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/github", content=body, headers=headers)


class FakeRunManager:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def resume(self, task_id, decision, feedback=None):
        if self.error:
            raise self.error(task_id)
        self.calls.append((task_id, decision, feedback))


def make_app(run_manager=None):
    services = make_services(settings=__import__("app.config", fromlist=["Settings"]).Settings(
        github_pat="p", github_webhook_secret=SECRET), redis=FakeRedis())
    app = create_app(services=services, graph=None)
    app.state.run_manager = run_manager or FakeRunManager()
    return app


@pytest.fixture
def app():
    return make_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_valid_review_event_resumes_task(client, app, monkeypatch):
    row = {"task_id": "t1", "repo": "org/repo", "status": "awaiting_approval"}

    async def fake_get_task_by_pr(dsn, repo, pr_number):
        return row
    monkeypatch.setattr(rw, "get_task_by_pr", fake_get_task_by_pr)
    resp = await _post(client, "pull_request_review",
                       {"action": "submitted", "pull_request": {"number": 9},
                        "review": {"state": "approved", "body": ""},
                        "repository": {"full_name": "org/repo"}})
    assert resp.status_code == 202
    assert app.state.run_manager.calls == [("t1", "approved", "")]


async def test_reject_comment_carries_feedback(client, app, monkeypatch):
    row = {"task_id": "t2", "repo": "org/repo", "status": "awaiting_approval"}

    async def fake_get_task_by_pr(dsn, repo, pr_number):
        return row
    monkeypatch.setattr(rw, "get_task_by_pr", fake_get_task_by_pr)
    resp = await _post(client, "issue_comment",
                       {"action": "created", "issue": {"number": 9, "pull_request": {"u": 1}},
                        "comment": {"body": "reject: bad approach", "user": {"type": "User"}},
                        "repository": {"full_name": "org/repo"}})
    assert resp.status_code == 202
    assert app.state.run_manager.calls == [("t2", "rejected", "bad approach")]


async def test_invalid_signature_is_400(client):
    resp = await _post(client, "pull_request_review", {"action": "submitted"},
                       secret="wrong")
    assert resp.status_code == 400
    assert client._transport.app.state.run_manager.calls == []


async def test_unknown_pr_returns_404(client, monkeypatch):
    async def none_task(dsn, repo, pr_number):
        return None
    monkeypatch.setattr(rw, "get_task_by_pr", none_task)
    resp = await _post(client, "pull_request_review",
                       {"action": "submitted", "pull_request": {"number": 99},
                        "review": {"state": "approved", "body": ""},
                        "repository": {"full_name": "org/repo"}})
    assert resp.status_code == 404


async def test_not_awaiting_returns_409(client, app, monkeypatch):
    app.state.run_manager = FakeRunManager(error=NotAwaitingApproval)
    row = {"task_id": "t3", "repo": "org/repo", "status": "done"}

    async def fake_get_task_by_pr(dsn, repo, pr_number):
        return row
    monkeypatch.setattr(rw, "get_task_by_pr", fake_get_task_by_pr)
    resp = await _post(client, "pull_request_review",
                       {"action": "submitted", "pull_request": {"number": 9},
                        "review": {"state": "approved", "body": ""},
                        "repository": {"full_name": "org/repo"}})
    assert resp.status_code == 409


async def test_non_object_json_body_returns_400(client, monkeypatch):
    body = b"[1, 2, 3]"
    headers = {"x-hub-signature-256": "sha256=" + hmac_mod.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": "pull_request_review",
        "content-type": "application/json"}
    resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid json"
    assert client._transport.app.state.run_manager.calls == []


async def test_ignored_events_return_200(client, app):
    resp = await _post(client, "push", {"action": "x"})
    assert resp.status_code == 200
    assert app.state.run_manager.calls == []
