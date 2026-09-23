from app.graph.nodes.planner import planner_node
from tests.fakes import StubLLM, make_services

PLAN_TEXT = "PLAN: 1. fix add() in calc.py\n2. run pytest"


async def test_planner_sets_plan_and_events():
    s = make_services(llm=StubLLM([PLAN_TEXT]))
    state = {"task_id": "t1", "task_description": "fix calc", "repo": "org/repo",
             "base_branch": "main", "model_overrides": {}, "cost_so_far": 0.0,
             "approval_status": "pending", "error_log": []}
    updates = await planner_node(state, services=s)
    assert updates["plan"] == PLAN_TEXT
    assert updates["status"] == "researching"
    assert updates["cost_so_far"] == 0.001
    types = [e.type for e in s.publisher.events]
    assert types[0] == "node_started" and types[-1] == "node_completed"
    assert any(e.type == "tool_call" for e in s.publisher.events)


async def test_planner_reentry_includes_prior_plan():
    llm = StubLLM(["REVISED PLAN"])
    s = make_services(llm=llm)
    state = {"task_id": "t1", "task_description": "d", "repo": "org/repo", "base_branch": "main",
             "model_overrides": {}, "cost_so_far": 0.0, "approval_status": "rejected",
             "plan": "old plan", "error_log": []}
    await planner_node(state, services=s)
    sent = llm.calls[0]["messages"][-1]["content"]
    assert "old plan" in sent and "revise" in sent.lower()