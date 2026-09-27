import asyncio
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from tests.fakes import FakeRedis, StubAgent, StubSandbox, make_services
from app.graph.state import TaskState


def make_one_node_graph():
    def passthrough(state):
        return {"status": "planning"}
    b = StateGraph(TaskState)
    b.add_node("planner", passthrough)
    b.add_edge(START, "planner")
    b.add_edge("planner", END)
    return b.compile(checkpointer=InMemorySaver())


def make_state(task_id="t-agent"):
    return {"task_id": task_id, "repo": "o/r", "task_description": "d",
            "base_branch": "main", "test_command": "pytest -q", "plan": None,
            "research_notes": None, "code_diff": None, "test_results": None,
            "review_comments": None, "approval_status": "pending",
            "retry_counts": {}, "cost_so_far": 0.0, "status": "planning",
            "error_log": [], "model_overrides": {}, "paused_at": None,
            "resumed_at": None, "pr_url": None, "template_id": "codex"}


async def test_drive_creates_and_sleeps_agent():
    agent = StubAgent()
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    from app.services.run_manager import RunManager
    rm = RunManager(s, make_one_node_graph())
    st = make_state()
    await rm.prepare("t-agent", st)
    await rm.start("t-agent", st)
    await asyncio.wait_for(rm._drives["t-agent"], timeout=5)
    assert agent.created == ["codex"]
    assert agent.waited == ["agent-1"]
    assert agent.slept == ["agent-1"]


async def test_restart_drive_recovers_agent_without_recreating():
    agent = StubAgent()
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    from app.services.run_manager import RunManager
    rm = RunManager(s, make_one_node_graph())
    st = make_state()
    await rm.prepare("t-agent", st)
    await rm.start("t-agent", st)
    await asyncio.wait_for(rm._drives["t-agent"], timeout=5)
    await rm.drive("t-agent", None)
    assert agent.created == ["codex"]
    assert agent.waited == ["agent-1"]
    assert agent.slept == ["agent-1", "agent-1"]