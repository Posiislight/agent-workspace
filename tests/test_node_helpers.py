from tests.fakes import make_services


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
    assert events[0].data["cost_so_far"] == 0.001
    assert events[0].data["llm_cost"] == 0.001
    assert events[0].data["vm_cost"] == 0.0