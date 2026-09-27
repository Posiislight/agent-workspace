from app.services.cost_tracker import CostTracker
from tests.fakes import FakeRedis


async def test_add_llm_cost_accumulates_in_redis():
    redis = FakeRedis()
    t = CostTracker(redis)
    assert await t.add_llm_cost("t1", 0.001) == 0.001
    assert await t.add_llm_cost("t1", 0.002) == 0.003
    assert float(redis.store["aw:t1:cost"]) == 0.003


async def test_add_llm_cost_without_redis_is_zero():
    t = CostTracker(None)
    assert await t.add_llm_cost("t1", 0.5) == 0.0


async def test_vm_awake_window_accrues_at_rate():
    import asyncio

    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_hour=60.0)
    t.vm_awake_start("t1")
    await asyncio.sleep(0.01)
    delta = await t.vm_awake_stop("t1")
    assert delta > 0
    assert 0 < float(redis.store["aw:t1:vm_cost"]) <= 0.1
    assert await t.vm_awake_stop("t1") == 0.0  # window already closed


async def test_vm_awake_stop_without_start_is_zero():
    t = CostTracker(FakeRedis())
    assert await t.vm_awake_stop("t1") == 0.0


async def test_add_vm_minutes_converts_rate_and_tracks_minutes():
    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_hour=6.0)
    delta = await t.add_vm_minutes("t1", 10.0)
    assert delta == 1.0
    assert float(redis.store["aw:t1:vm_minutes"]) == 10.0
    assert float(redis.store["aw:t1:vm_cost"]) == 1.0


async def test_snapshot_returns_all_components():
    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_hour=6.0)
    await t.add_llm_cost("t1", 1.25)
    await t.add_vm_minutes("t1", 10.0)
    snap = await t.snapshot("t1")
    assert snap == {"llm_cost": 1.25, "vm_minutes": 10.0, "vm_cost": 1.0}
    assert await CostTracker(None).snapshot("t1") == {
        "llm_cost": 0.0, "vm_minutes": 0.0, "vm_cost": 0.0}
