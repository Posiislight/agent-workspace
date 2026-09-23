import json

import httpx
import pytest

from app.config import Settings
from app.sandbox.computers import HumanInControl, MaritimeComputers, MaritimePlanError


class ComputersMock:
    def __init__(self):
        self.created = []
        self.actions = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if "/409" in path:  # the "c1/409" computer is held by a human (checked before endpoint suffixes)
            return httpx.Response(409, json={"error": "human_in_control"})
        if method == "POST" and path == "/api/v1/computers":
            body = json.loads(request.content)
            self.created.append(body)
            if body.get("externalUserId") == "t402":
                return httpx.Response(402, json={"error": "computer_limit", "message": "need paid plan"})
            return httpx.Response(201, json={"computerId": "c1", "frameId": 0,
                                             "width": 1200, "height": 750})
        if method == "POST" and path.endswith("/actions"):
            self.actions.append(json.loads(request.content))
            return httpx.Response(200, json={"frameId": 5, "width": 1200, "height": 750})
        if method == "POST" and path.endswith("/exec"):
            return httpx.Response(200, json={"exitCode": 0, "stdout": "ok", "stderr": ""})
        if method == "POST" and path.endswith("/viewer"):
            return httpx.Response(200, json={"url": "https://view.maritime.sh/abc"})
        if method == "POST" and path.endswith("/sleep"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unmocked", "path": path})


def make(mock):
    client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler),
                               base_url="https://api.maritime.sh")
    return MaritimeComputers(Settings(maritime_api_key="mk"), client)


async def test_ensure_get_or_create_sends_external_user_id():
    m = ComputersMock()
    mc = make(m)
    info = await mc.ensure("t1")
    assert info.computer_id == "c1"
    assert m.created == [{"externalUserId": "t1", "name": "aw-task-t1-research"}]


async def test_act_viewer_and_shell():
    m = ComputersMock()
    mc = make(m)
    info = await mc.act("c1", {"action": "screenshot"})
    assert info.frame_id == 5
    assert await mc.viewer_link("c1") == "https://view.maritime.sh/abc"
    res = await mc.shell("c1", "ls")
    assert res.exit_code == 0


async def test_open_url_runs_chromium_then_waits():
    m = ComputersMock()
    mc = make(m)
    await mc.open_url("c1", "https://docs.example.com")
    assert any("chromium" in a.get("command", "") for a in m.actions) is False  # shell goes via exec
    assert {"action": "wait", "duration": 3} in m.actions


async def test_402_maps_to_plan_error():
    mc = make(ComputersMock())

    class PaidGate(ComputersMock):
        def handler(self, request):
            if request.url.path == "/api/v1/computers":
                return httpx.Response(402, json={"error": "computer_limit", "message": "paid plan required"})
            return super().handler(request)

    mc2 = MaritimeComputers(Settings(maritime_api_key="k"),
                            httpx.AsyncClient(transport=httpx.MockTransport(PaidGate().handler),
                                              base_url="https://api.maritime.sh"))
    with pytest.raises(MaritimePlanError):
        await mc2.ensure("t402")


async def test_409_maps_to_human_in_control():
    client = httpx.AsyncClient(transport=httpx.MockTransport(ComputersMock().handler),
                               base_url="https://api.maritime.sh")
    mc = MaritimeComputers(Settings(maritime_api_key="k"), client)
    with pytest.raises(HumanInControl):
        await mc.act("c1/409", {"action": "screenshot"})
