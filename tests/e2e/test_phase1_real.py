import os

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

pytestmark = pytest.mark.e2e

requires_keys = pytest.mark.skipif(
    not (os.getenv("OPENROUTER_API_KEY") and os.getenv("MARITIME_API_KEY")
         and os.getenv("MARITIME_TEMPLATE_ID") and os.getenv("REPO_UNDER_TEST")),
    reason="set OPENROUTER_API_KEY, MARITIME_API_KEY, MARITIME_TEMPLATE_ID, "
           "REPO_UNDER_TEST (org/name of a GitHub repo created from demo/sample-repo)")


class ListPublisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return "0-1"


@requires_keys
async def test_phase1_end_to_end():
    from app.config import get_settings
    from app.graph.build import build_graph
    from app.graph.nodes.helpers import Services
    from app.graph.state import initial_state
    from app.llm.openrouter import OpenRouterClient
    from app.llm.pricing import PriceTable
    from app.sandbox.computers import MaritimeComputers
    from app.sandbox.maritime import make_sandbox

    settings = get_settings()
    publisher = ListPublisher()
    http_open = httpx.AsyncClient(base_url=settings.openrouter_base_url, timeout=130)
    http_maritime = httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
    prices = await PriceTable.fetch(http_open)
    sandbox_cache: dict[str, object] = {}

    def _sandbox_for(st):
        tid = st["task_id"]
        if tid not in sandbox_cache:
            sandbox_cache[tid] = make_sandbox(settings, tid, st["repo"], st["base_branch"],
                                              client=http_maritime)
        return sandbox_cache[tid]

    svc = Services(
        settings=settings,
        llm=OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url, http_open),
        prices=prices,
        publisher=publisher,
        sandbox_factory=_sandbox_for,
        computers=MaritimeComputers(settings, http_maritime),
        redis=None,
    )
    graph = build_graph(svc, InMemorySaver())
    config = {"configurable": {"thread_id": "e2e-phase1"}}
    state = dict(initial_state(
        "e2e-phase1",
        "Fix the failing test in tests/test_calc.py so add(a, b) returns the sum.",
        os.environ["REPO_UNDER_TEST"], "main", "pytest -q"))

    interrupted = False
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupted = True
            break
    assert interrupted

    events = publisher.events
    assert any(e.node == "researcher" and e.data.get("action") == "browser_open" for e in events)
    assert any(e.data.get("viewer_url") for e in events)
    assert any(e.type == "cost_update" for e in events)

    async for _chunk in graph.astream(Command(resume={"decision": "approved", "feedback": ""}),
                                      config, stream_mode="updates"):
        pass
    snap = await graph.aget_state(config)
    assert snap.values["status"] == "done"
    assert snap.values["test_results"]["passed"] is True
    await http_open.aclose()
    await http_maritime.aclose()
