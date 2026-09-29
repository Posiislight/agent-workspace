import asyncio
import json
from types import SimpleNamespace

import pytest
from langgraph.types import Command

from app.services.run_manager import (
    AlreadyRunning,
    NotRestartable,
    RunManager,
    TaskNotFound,
)
from tests.fakes import FakeRedis, make_services


class StubGraph:
    """Records astream inputs; aget_state mirrors what the last chunk 'wrote'."""

    def __init__(self):
        self.inputs: list[object] = []
        self.values: dict = {}

    async def astream(self, graph_input, _config, stream_mode=None):
        self.inputs.append(graph_input)
        self.values = {"task_id": "t1", "repo": "org/repo", "status": "done"}
        yield {"node": self.values}

    async def aget_state(self, _config):
        return SimpleNamespace(values=self.values) if self.values else None


def _seed(rm, task_id, status):
    return rm._mirror(task_id, {"task_id": task_id, "repo": "org/repo",
                                "status": status})


async def test_restart_re_drives_failed_task():
    graph = StubGraph()
    rm = RunManager(make_services(redis=FakeRedis()), graph)
    await _seed(rm, "t1", "failed")
    await rm.restart("t1")
    drive = rm._drives["t1"]
    await drive
    # No agent anywhere yet: one is created and its id handed to the graph.
    assert graph.inputs == [Command(update={"agent_id": "agent-1"})]
    state = json.loads(await rm.services.redis.get("aw:t1:state"))
    assert state["status"] == "done"
    assert state["repo"] == "org/repo"


async def test_restart_re_drives_needs_human_task():
    graph = StubGraph()
    rm = RunManager(make_services(redis=FakeRedis()), graph)
    await _seed(rm, "t2", "needs_human")
    await rm.restart("t2")
    await rm._drives["t2"]
    # No agent anywhere yet: one is created and its id handed to the graph.
    assert graph.inputs == [Command(update={"agent_id": "agent-1"})]


async def test_restart_emits_restarted_event():
    services = make_services(redis=FakeRedis())
    rm = RunManager(services, StubGraph())
    await _seed(rm, "t3", "failed")
    await rm.restart("t3")
    await rm._drives["t3"]
    events = [e for e in services.publisher.events if e.type == "node_started"
              and e.node == "run_manager"]
    assert events and events[-1].data == {"restarted": True}


async def test_restart_unknown_task_raises():
    rm = RunManager(make_services(redis=FakeRedis()), StubGraph())
    with pytest.raises(TaskNotFound):
        await rm.restart("missing")


async def test_restart_not_restartable_status_raises():
    rm = RunManager(make_services(redis=FakeRedis()), StubGraph())
    for status in ("done", "awaiting_approval", "planning"):
        await _seed(rm, f"t-{status}", status)
        with pytest.raises(NotRestartable):
            await rm.restart(f"t-{status}")


async def test_restart_while_running_raises():
    class SlowGraph:
        async def astream(self, *_args, **_kw):
            await asyncio.sleep(0.05)
            yield {}

        async def aget_state(self, *_args, **_kw):
            return None

    rm = RunManager(make_services(redis=FakeRedis()), SlowGraph())
    await _seed(rm, "t-live", "failed")
    await rm.start("t-live", {"task_id": "t-live"})
    with pytest.raises(AlreadyRunning):
        await rm.restart("t-live")
