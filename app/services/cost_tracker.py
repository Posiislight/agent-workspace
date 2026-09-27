import time


class CostTracker:
    """Per-task cost accumulator: LLM dollars + VM awake-minutes (spec §10)."""

    def __init__(self, redis, vm_rate_per_hour: float = 0.0):
        self._redis = redis
        self._rate = vm_rate_per_hour
        self._awake_since: dict[str, float] = {}

    async def add_llm_cost(self, task_id: str, delta: float) -> float:
        if self._redis is None:
            return 0.0
        return float(await self._redis.incrbyfloat(f"aw:{task_id}:cost", delta))

    def vm_awake_start(self, task_id: str) -> None:
        self._awake_since[task_id] = time.monotonic()

    async def vm_awake_stop(self, task_id: str) -> float:
        started = self._awake_since.pop(task_id, None)
        if started is None:
            return 0.0
        minutes = (time.monotonic() - started) / 60.0
        return await self.add_vm_minutes(task_id, minutes)

    async def add_vm_minutes(self, task_id: str, minutes: float) -> float:
        if self._redis is None:
            return 0.0
        await self._redis.incrbyfloat(f"aw:{task_id}:vm_minutes", minutes)
        vm_cost = round(minutes * self._rate / 60.0, 6)
        if vm_cost:
            await self._redis.incrbyfloat(f"aw:{task_id}:vm_cost", vm_cost)
        return vm_cost

    async def snapshot(self, task_id: str) -> dict:
        if self._redis is None:
            return {"llm_cost": 0.0, "vm_minutes": 0.0, "vm_cost": 0.0}
        async def _f(key):
            raw = await self._redis.get(key)
            return float(raw) if raw else 0.0
        return {"llm_cost": await _f(f"aw:{task_id}:cost"),
                "vm_minutes": await _f(f"aw:{task_id}:vm_minutes"),
                "vm_cost": await _f(f"aw:{task_id}:vm_cost")}
