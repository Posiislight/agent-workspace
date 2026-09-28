import asyncio
import re

import httpx

from app.llm.backoff import with_retry


class AgentTimeout(RuntimeError):
    pass


# Harness chat is async under the hood: after ~25s the gateway replies with an
# acknowledgement while the harness keeps working; messages to that
# conversation then get a "still working" reply until the answer is ready, and
# the next message after completion receives it. Other conversations are
# unaffected. (Verified against live codex/dsh agents; not in Maritime docs.)
_PENDING = re.compile(
    r"^\s*(I'm on it\b.*message me again|Still working on your last request\b)",
    re.I | re.S)
_NUDGE = "Are you finished? If so, reply with your complete answer to my previous request."


def is_pending_reply(text: str | None) -> bool:
    return bool(text) and bool(_PENDING.match(text))


class MaritimeAgentClient:
    """Maritime REST wrapper: agent lifecycle + harness chat (LLM proxy)."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url="https://api.maritime.sh", timeout=600)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._retry_base = 1.0
        self._poll_s = 10.0

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
                   timeout_s: float = 1800) -> str:
        """Send `message` and return the harness's final answer.

        Polls the same conversation through ack/"still working" replies until
        the real answer arrives or `timeout_s` elapses (AgentTimeout).
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        reply = await self._chat_once(agent_id, message, conversation_id)
        while is_pending_reply(reply):
            if loop.time() >= deadline:
                raise AgentTimeout(
                    f"agent {agent_id} still working on {conversation_id} "
                    f"after {timeout_s}s")
            await asyncio.sleep(self._poll_s)
            reply = await self._chat_once(agent_id, _NUDGE, conversation_id)
        return reply

    async def _chat_once(self, agent_id: str, message: str, conversation_id: str) -> str:
        async def _call() -> str:
            r = await self._req("POST", f"/api/agents/{agent_id}/chat",
                                json={"message": message,
                                      "conversation_id": conversation_id},
                                timeout=180)
            r.raise_for_status()
            d = r.json()
            if d.get("response") is None:
                raise RuntimeError(f"agent chat error: {d.get('error') or d}")
            return d["response"]
        # A retried POST after a gateway error is safe: if the first attempt
        # is still running, the harness answers "still working" and we poll.
        return await with_retry(_call, base_delay=self._retry_base)

    async def llm_status(self, agent_id: str) -> dict:
        r = await self._req("GET", f"/api/agents/{agent_id}/llm-status")
        r.raise_for_status()
        return r.json()

    async def sleep(self, agent_id: str) -> None:
        await self._req("POST", f"/api/agents/{agent_id}/sleep")

    async def total_compute_seconds(self, agent_id: str) -> float:
        return float((await self.get(agent_id)).get("totalComputeSeconds") or 0.0)
