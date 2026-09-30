import httpx

from app.main import create_app
from app.services.run_manager import AlreadyRunning
from tests.fakes import make_services


class BusyRunManager:
    async def resume(self, task_id, decision, feedback=None):
        raise AlreadyRunning(task_id)


async def test_second_approve_while_resuming_is_409_not_500():
    # A double-click sends two approves; the second hits the in-flight resume.
    app = create_app(services=make_services(), graph=None)
    app.state.run_manager = BusyRunManager()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks/t1/approve", json={})
    assert r.status_code == 409
