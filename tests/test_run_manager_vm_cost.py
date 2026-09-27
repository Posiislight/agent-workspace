import json

from langgraph.checkpoint.memory import InMemorySaver

from app.graph.build import build_graph
from app.graph.state import initial_state
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services

OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py",
                           "content": "x = 1"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com/api", "notes",
          OPS, APPROVED]


def _services(redis, rate=6.0):
    from app.config import Settings
    return make_services(llm=StubLLM(list(SCRIPT)), sandbox=StubSandbox(),
                         github=StubGitHub(), redis=redis,
                         settings=Settings(github_pat="p",
                                           vm_cost_per_hour=rate))


async def test_drive_accrues_vm_cost_and_mirrors_totals():
    from tests.fakes import FakeRedis
    redis = FakeRedis()
    services = _services(redis)
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    await rm.drive("t-vm", dict(initial_state("t-vm", "fix add", "org/repo",
                                              "main", "pytest -q", {})))
    snap = await services.cost.snapshot("t-vm")
    assert snap["llm_cost"] > 0          # 5 LLM calls (planner, researcher x2, coding, reviewer) * stub price
    assert snap["vm_minutes"] > 0
    assert snap["vm_cost"] > 0
    mirrored = json.loads(await redis.get("aw:t-vm:state"))
    assert mirrored["llm_cost"] == snap["llm_cost"]
    assert mirrored["vm_cost"] == snap["vm_cost"]
    assert mirrored["vm_minutes"] == snap["vm_minutes"]
    assert services.cost._awake_since == {}  # window closed at drive end
