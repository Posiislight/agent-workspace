class PriceTable:
    """Per-model price in USD per token, parsed from OpenRouter /models strings."""

    def __init__(self, prices: dict[str, dict[str, float]]):
        self._prices = prices

    @classmethod
    async def fetch(cls, client) -> "PriceTable":
        r = await client.get("/models")
        r.raise_for_status()
        prices: dict[str, dict[str, float]] = {}
        for m in r.json()["data"]:
            p = m.get("pricing") or {}
            try:
                prices[m["id"]] = {"prompt": float(p.get("prompt", 0) or 0),
                                   "completion": float(p.get("completion", 0) or 0)}
            except (TypeError, ValueError):
                continue
        return cls(prices)

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = self._prices.get(model)
        if not p:
            return 0.0
        return prompt_tokens * p["prompt"] + completion_tokens * p["completion"]
