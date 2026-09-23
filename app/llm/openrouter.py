import json
from typing import Callable

import httpx


class LLMResult:
    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int, model: str):
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1",
                 client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=120)
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "HTTP-Referer": "https://agent-workspace.local",
                         "X-Title": "agent-workspace"}

    async def chat(self, model: str, messages: list[dict],
                   stream_cb: Callable[[str], None] | None = None) -> LLMResult:
        body = {"model": model, "messages": messages}
        if stream_cb is not None:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        if stream_cb is None:
            r = await self._client.post("/chat/completions", json=body, headers=self._headers)
            r.raise_for_status()
            d = r.json()
            u = d.get("usage") or {}
            return LLMResult(d["choices"][0]["message"]["content"],
                             u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model)

        text_parts: list[str] = []
        usage: dict = {}
        async with self._client.stream("POST", "/chat/completions", json=body,
                                       headers=self._headers) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {}).get("content") or ""
                    if delta:
                        text_parts.append(delta)
                        stream_cb(delta)
        return LLMResult("".join(text_parts), usage.get("prompt_tokens", 0),
                         usage.get("completion_tokens", 0), model)

    async def models(self) -> list[dict]:
        r = await self._client.get("/models", headers=self._headers)
        r.raise_for_status()
        return r.json()["data"]
