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
            "error_log": [], "paused_at": None,
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

def make_done_graph():
    b = StateGraph(TaskState)
    b.add_node("commit_pr", lambda state: {"status": "done"})
    b.add_edge(START, "commit_pr")
    b.add_edge("commit_pr", END)
    return b.compile(checkpointer=InMemorySaver())


async def test_finished_task_deletes_agent_instead_of_sleeping():
    # A done task never runs again; a sleeping agent would hold a plan slot.
    agent = StubAgent()
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    from app.services.run_manager import RunManager
    rm = RunManager(s, make_done_graph())
    st = make_state()
    await rm.prepare("t-agent", st)
    await rm.start("t-agent", st)
    await asyncio.wait_for(rm._drives["t-agent"], timeout=5)
    assert agent.deleted == ["agent-1"]
    assert agent.slept == []


async def test_restart_recreates_agent_that_no_longer_exists():
    from langgraph.types import Command

    from app.services.run_manager import RunManager

    class RecordingGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, graph_input, _config, stream_mode=None):
            self.inputs.append(graph_input)
            return
            yield

        async def aget_state(self, _config):
            return None

    agent = StubAgent(agent_id="agent-new")
    agent.missing.add("agent-old")
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    graph = RecordingGraph()
    rm = RunManager(s, graph)
    await rm.prepare("t-agent", {**make_state(), "status": "failed",
                                 "agent_id": "agent-old"})
    await rm.drive("t-agent", None)
    assert agent.created == ["codex"]
    assert agent.waited == ["agent-new"]
    # The new id must reach graph state without disturbing the pending node,
    # which a plain aupdate_state would (it drops a Command goto).
    (inp,) = graph.inputs
    assert isinstance(inp, Command) and inp.update == {"agent_id": "agent-new"}


async def test_restart_takes_agent_id_from_checkpoint_when_mirror_lacks_it():
    # A node that crashes before its first update leaves the Redis mirror with
    # only the prepare() values; the checkpoint still has the drive's agent_id.
    from types import SimpleNamespace

    from app.services.run_manager import RunManager

    class CheckpointGraph:
        def __init__(self):
            self.inputs = []

        async def astream(self, graph_input, _config, stream_mode=None):
            self.inputs.append(graph_input)
            return
            yield

        async def aget_state(self, _config):
            return SimpleNamespace(values={"agent_id": "agent-ckpt"})

    agent = StubAgent()
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    graph = CheckpointGraph()
    rm = RunManager(s, graph)
    await rm.prepare("t-agent", {**make_state(), "status": "failed"})
    await rm.drive("t-agent", None)
    assert agent.created == []
    assert graph.inputs == [None]
    assert agent.slept == ["agent-ckpt"]
