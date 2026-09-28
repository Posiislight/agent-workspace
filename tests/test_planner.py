from tests.fakes import StubAgent, StubSandbox, make_services

from app.graph.nodes.planner import planner_node


def make_state(**kw):
    st = {"task_id": "t1", "task_description": "fix calc", "repo": "org/repo",
          "base_branch": "main", "agent_id": "agent-1", "template_id": "codex",
          "approval_status": "pending", "error_log": [],
          "retry_counts": {"testing": 0, "coding": 0}}
    st.update(kw)
    return st


async def test_planner_sets_plan_and_events():
    stub = StubAgent(responses=["PLAN TEXT"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    updates = await planner_node(make_state(), services=s)
    assert updates["plan"] == "PLAN TEXT"
    call = stub.calls[0]
    assert call["conversation_id"] == "aw-t1-planner"
    assert "Task:" in call["message"]
    assert call["agent_id"] == "agent-1"
    assert updates["status"] == "researching"
    types = [e.type for e in s.publisher.events]
    assert types[0] == "node_started" and types[-1] == "node_completed"
    assert any(e.type == "tool_call" for e in s.publisher.events)


async def test_planner_reentry_includes_prior_plan():
    stub = StubAgent(responses=["REVISED PLAN"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    state = make_state(approval_status="rejected", plan="old plan")
    await planner_node(state, services=s)
    sent = stub.calls[0]["message"]
    assert "old plan" in sent and "revise" in sent.lower()
