import httpx
import pytest

from app.main import create_app
from app.services.run_manager import (
    AlreadyRunning,
    NotAwaitingApproval,
    NotRestartable,
    TaskNotFound,
)
from tests.fakes import FakeRedis, make_services


class RestartRunManager:
    def __init__(self, error=None):
        self.calls: list[str] = []
        self.error = error

    async def restart(self, task_id):
        if self.error:
            raise self.error(task_id)
        self.calls.append(task_id)

    async def get_state(self, task_id):
        return {"task_id": task_id, "status": "failed"}


def make_app(run_manager):
    services = make_services(redis=FakeRedis())
    app = create_app(services=services, graph=None)
    app.state.run_manager = run_manager
    return app


@pytest.fixture
async def client():
    app = make_app(RestartRunManager())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, app


async def test_restart_returns_202(client):
    c, app = client
    resp = await c.post("/tasks/t1/restart")
    assert resp.status_code == 202
    assert resp.json() == {"restarted": True}
    assert app.state.run_manager.calls == ["t1"]


async def test_restart_unknown_task_returns_404(client):
    c, app = client
    app.state.run_manager = RestartRunManager(error=TaskNotFound)
    resp = await c.post("/tasks/nope/restart")
    assert resp.status_code == 404


async def test_restart_not_restartable_returns_409(client):
    c, app = client
    app.state.run_manager = RestartRunManager(error=NotRestartable)
    resp = await c.post("/tasks/t1/restart")
    assert resp.status_code == 409


async def test_restart_already_running_returns_409(client):
    c, app = client
    app.state.run_manager = RestartRunManager(error=AlreadyRunning)
    resp = await c.post("/tasks/t1/restart")
    assert resp.status_code == 409


async def test_restart_not_awaiting_returns_409(client):
    c, app = client
    app.state.run_manager = RestartRunManager(error=NotAwaitingApproval)
    resp = await c.post("/tasks/t1/restart")
    assert resp.status_code == 409
