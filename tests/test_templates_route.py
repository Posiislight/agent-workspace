from fastapi.testclient import TestClient
from types import SimpleNamespace

from app.main import create_app


class StubRunManager:
    def __init__(self):
        self.started = []

    async def prepare(self, task_id, state):
        self.started.append(("prepare", task_id))

    async def start(self, task_id, state):
        self.started.append(("start", task_id))


def make_client():
    app = create_app(services=SimpleNamespace(
        settings=SimpleNamespace(github_pat="k", templates_allowlist=("codex", "dsh")),
        llm=None))
    return TestClient(app)


def test_templates_lists_allowlist():
    c = make_client()
    r = c.get("/templates")
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["templates"]]
    assert ids == ["codex", "dsh"]


def make_task_client(template_id="codex"):
    app = create_app(services=SimpleNamespace(
        settings=SimpleNamespace(github_pat="k", templates_allowlist=("codex", "dsh")),
        llm=None))
    app.state.run_manager = StubRunManager()
    return TestClient(app), app


def test_create_task_rejects_unknown_template():
    c, _app = make_task_client()
    r = c.post("/tasks", json={"task_description": "fix add", "repo": "org/repo",
                               "template_id": "claude_code"})
    assert r.status_code == 422


def test_create_task_accepts_known_template():
    c, app = make_task_client()
    r = c.post("/tasks", json={"task_description": "fix add", "repo": "org/repo",
                               "template_id": "dsh"})
    assert r.status_code == 201
    assert ("prepare", r.json()["task_id"]) in app.state.run_manager.started


def test_create_task_missing_template_rejected():
    c, _app = make_task_client()
    r = c.post("/tasks", json={"task_description": "fix add", "repo": "org/repo"})
    assert r.status_code == 422


def test_templates_defaults_without_allowlist():
    app = create_app(services=SimpleNamespace(
        settings=SimpleNamespace(github_pat="k"), llm=None))
    c = TestClient(app)
    r = c.get("/templates")
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["templates"]]
    assert ids == ["codex", "dsh"]