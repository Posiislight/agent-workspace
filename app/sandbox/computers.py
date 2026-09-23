import shlex

import httpx

from app.sandbox.base import ExecResult


class MaritimePlanError(RuntimeError):
    """Computers require a paid Maritime plan (402 computer_limit/no_plan/plan_lapsed)."""


class HumanInControl(RuntimeError):
    """A person holds the desktop (409) — wait, then retry."""


class ComputerInfo:
    def __init__(self, d: dict):
        self.computer_id = d.get("computerId") or d.get("id")
        self.frame_id = d.get("frameId")
        self.width = d.get("width")
        self.height = d.get("height")
        self.raw = d


class MaritimeComputers:
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self._s = settings
        self._client = client or httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=90)

    def _headers(self):
        return {"Authorization": f"Bearer {self._s.maritime_api_key}"}

    async def _req(self, method: str, path: str, body=None) -> httpx.Response:
        r = await self._client.request(method, path, headers=self._headers(), json=body)
        if r.status_code == 402:
            raise MaritimePlanError(r.text[:300])
        if r.status_code == 409 and "human_in_control" in r.text:
            raise HumanInControl(r.text[:300])
        r.raise_for_status()
        return r

    async def ensure(self, task_id: str) -> ComputerInfo:
        r = await self._req("POST", "/api/v1/computers",
                            {"externalUserId": task_id, "name": f"aw-task-{task_id}-research"})
        return ComputerInfo(r.json())

    async def act(self, computer_id: str, action: dict) -> ComputerInfo:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/actions", action)
        return ComputerInfo(r.json())

    async def screenshot(self, computer_id: str) -> tuple[bytes, str | None]:
        r = await self._client.get(f"/api/v1/computers/{computer_id}/screenshot",
                                   headers=self._headers())
        r.raise_for_status()
        return r.content, r.headers.get("X-Frame-Id")

    async def shell(self, computer_id: str, command: str, timeout_s: int = 30) -> ExecResult:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/exec",
                            {"command": command, "timeoutS": timeout_s})
        d = r.json()
        return ExecResult(d.get("exitCode", 1), d.get("stdout", ""), d.get("stderr", ""))

    async def open_url(self, computer_id: str, url: str) -> ComputerInfo:
        await self.shell(computer_id, f"nohup chromium --new-window {shlex.quote(url)} >/dev/null 2>&1 &")
        return await self.act(computer_id, {"action": "wait", "duration": 3})

    async def viewer_link(self, computer_id: str, mode: str = "watch", ttl_s: int = 600) -> str:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/viewer",
                            {"mode": mode, "ttlS": ttl_s})
        return r.json().get("url", "")

    async def sleep(self, computer_id: str) -> None:
        await self._req("POST", f"/api/v1/computers/{computer_id}/sleep", {})
