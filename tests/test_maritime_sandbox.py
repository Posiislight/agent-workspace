import json

import httpx
import pytest

from app.config import Settings
from app.sandbox.maritime import MaritimeSandbox

pytestmark = pytest.mark.integration


class MaritimeMock:
    """In-memory Maritime API: one agent namespace, files dict, scripted exec."""

    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.next_id = 1
        self.exec_calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/agents":
            body = json.loads(request.content)
            aid = f"a{self.next_id}"
            self.next_id += 1
            self.agents[aid] = body
            return httpx.Response(201, json={"id": aid, "name": body["name"]})
        if method == "GET" and path.endswith("/download"):
            aid = path.split("/")[-2]
            p = request.url.params.get("path", "")
            return httpx.Response(200, content=self.files.get((aid, p), "").encode())
        if method == "GET" and path.startswith("/api/agents/"):
            aid = path.split("/")[-1]
            if aid in self.agents:
                return httpx.Response(200, json={"id": aid, "status": "running"})
            return httpx.Response(404, json={"error": "not_found"})
        if method == "POST" and path.endswith("/exec"):
            body = json.loads(request.content)
            return httpx.Response(200, json=self._exec(body["command"]))
        if method == "PUT" and path.endswith("/write"):
            aid = path.split("/")[-2]
            body = json.loads(request.content)
            self.files[(aid, body["path"])] = body["content"]
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path.endswith("/sleep"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unmocked", "path": path})

    def _exec(self, command) -> dict:
        raw = command if isinstance(command, str) else " ".join(command)
        self.exec_calls.append(raw)
        if "git clone" in raw:
            return {"exitCode": 0, "stdout": "cloned", "stderr": ""}
        if "git diff --cached" in raw and "git add" in raw:
            return {"exitCode": 0, "stdout": "diff --git a/f.py b/f.py\n+print(1)", "stderr": ""}
        if raw.startswith("nohup"):
            return {"exitCode": 0, "stdout": "1234", "stderr": ""}
        if ".code" in raw:  # poll: exit-code file present -> done, code 0
            return {"exitCode": 0, "stdout": "DONE\n0", "stderr": ""}
        if raw.startswith("tail -c"):
            return {"exitCode": 0, "stdout": "1 passed in 0.01s", "stderr": ""}
        return {"exitCode": 0, "stdout": "ok", "stderr": ""}


def make(m: MaritimeMock) -> tuple[Settings, httpx.AsyncClient, MaritimeSandbox]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(m.handler),
                               base_url="https://api.maritime.sh")
    s = Settings(maritime_api_key="mk_test", maritime_template_id="t-code",
                 sandbox_poll_interval_seconds=0.0)
    sb = MaritimeSandbox(s, client, "t1", "org/repo", base_branch="main")
    return s, client, sb


async def test_ensure_creates_then_reuses():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    aid1 = await sb.ensure()
    aid2 = await sb.ensure()
    assert aid1 == aid2 == sb.agent_id
    assert m.agents[aid1]["name"] == "aw-task-t1"


async def test_ensure_recreates_after_404():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    aid1 = await sb.ensure()
    del m.agents[aid1]
    aid2 = await sb.ensure()
    assert aid2 != aid1
    assert any("git clone" in c for c in m.exec_calls)


async def test_provision_installs_venv_and_runs_dir():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    script = [c for c in m.exec_calls if "python3 -m venv" in c]
    assert script and "mkdir -p /data/.runs" in script[0]


async def test_run_long_returns_log_output():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    res = await sb.run_long("pytest -q", timeout=10)
    assert res.exit_code == 0
    assert "1 passed" in res.stdout


async def test_write_and_read_file():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    await sb.write_file("/data/workspace/calc.py", "x = 1")
    assert await sb.read_file("/data/workspace/calc.py") == "x = 1"


async def test_diff_uses_base_branch():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    d = await sb.diff()
    assert d.startswith("diff --git")
    assert any("git diff --cached main" in c for c in m.exec_calls)
