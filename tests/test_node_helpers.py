from tests.fakes import FakeRedis, make_services


async def test_model_for_override_wins():
    s = make_services()
    state = {"model_overrides": {"planner": "custom/model"}, "cost_so_far": 0.0}
    from app.graph.nodes.helpers import model_for
    assert model_for(s, state, "planner") == "custom/model"
    assert model_for(s, state, "reviewer") == s.settings.model_reviewer


async def test_apply_llm_cost_updates_state_and_emits():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    s = make_services()
    state = {"task_id": "t1", "cost_so_far": 0.0}
    result = LLMResult("x", 100, 10, "m")
    updates = await apply_llm_cost({"plan": "p"}, state, s, result, "planner")
    assert updates["cost_so_far"] == 0.001
    events = s.publisher.events
    assert len(events) == 1
    assert events[0].type == "cost_update"
    assert events[0].data == {"cost_so_far": 0.001}


async def test_apply_llm_cost_increments_redis_tracker():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    redis = FakeRedis()
    s = make_services(redis=redis)
    state = {"task_id": "t-cost", "cost_so_far": 0.0}
    updates = await apply_llm_cost({}, state, s, LLMResult("x", 100, 10, "m"), "planner")
    assert updates["cost_so_far"] == 0.001
    assert float(redis.store["aw:t-cost:cost"]) == 0.001


async def test_apply_llm_cost_without_tracker_still_works():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    s = make_services(cost=False)
    s.cost = None
    updates = await apply_llm_cost({}, {"task_id": "t", "cost_so_far": 0.0}, s,
                                   LLMResult("x", 100, 10, "m"), "planner")
    assert updates["cost_so_far"] == 0.001


async def test_make_services_autowires_tracker_from_redis():
    s = make_services(redis=FakeRedis())
    from app.services.cost_tracker import CostTracker
    assert isinstance(s.cost, CostTracker)