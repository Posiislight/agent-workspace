import asyncio

import httpx

from app.llm.backoff import with_retry


class AgentTimeout(RuntimeError):
    pass


class MaritimeAgentClient:
    """Maritime REST wrapper: agent lifecycle + harness chat (LLM proxy)."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url="https://api.maritime.sh", timeout=600)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._retry_base = 1.0

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        return await self._client.request(method, path, headers=self._headers, **kw)

    async def create(self, name: str, template_id: str) -> str:
        r = await self._req("POST", "/api/agents",
                            json={"name": name, "templateId": template_id})
        r.raise_for_status()
        return r.json()["id"]

    async def get(self, agent_id: str) -> dict:
        r = await self._req("GET", f"/api/agents/{agent_id}")
        r.raise_for_status()
        return r.json()

    async def wait_active(self, agent_id: str, timeout_s: float = 180,
                          poll_s: float = 5) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            d = await self.get(agent_id)
            if d.get("status") == "active":
                return agent_id
            if asyncio.get_event_loop().time() >= deadline:
                raise AgentTimeout(
                    f"agent {agent_id} not active after {timeout_s}s "
                    f"(status={d.get('status')})")
            await asyncio.sleep(poll_s)

    async def chat(self, agent_id: str, message: str, conversation_id: str,
                   timeout_s: int = 600) -> str:
        async def _call() -> str:
            r = await self._req("POST", f"/api/agents/{agent_id}/chat",
                                json={"message": message,
                                      "conversation_id": conversation_id},
                                timeout=timeout_s)
            r.raise_for_status()
            return r.json()["response"]
        return await with_retry(_call, base_delay=self._retry_base)

    async def llm_status(self, agent_id: str) -> dict:
        r = await self._req("GET", f"/api/agents/{agent_id}/llm-status")
        r.raise_for_status()
        return r.json()

    async def sleep(self, agent_id: str) -> None:
        await self._req("POST", f"/api/agents/{agent_id}/sleep")

    async def total_compute_seconds(self, agent_id: str) -> float:
        return float((await self.get(agent_id)).get("totalComputeSeconds") or 0.0)
