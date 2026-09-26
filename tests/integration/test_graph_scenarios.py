import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.build import build_graph
from app.graph.state import initial_state
from app.sandbox.base import ExecResult
from app.services.run_manager import RunManager
from tests.fakes import StubLLM, StubSandbox, make_services

pytestmark = pytest.mark.integration

URLS = "URL: https://docs.example.com/api"
NOTES = "notes with citations"
OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 1"}], "done": True})
OPS2 = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 2"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
NEEDS_CHANGES = json.dumps({"verdict": "needs_changes", "comments": ["check negative numbers"]})


def initial():
    return dict(initial_state("t-scen", "fix add", "org/repo", "main", "pytest -q", {}))


async def drive_to_interrupt(graph, tid, payload):
    config = {"configurable": {"thread_id": tid}}
    async for chunk in graph.astream(payload, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            return config
    return config


async def test_happy_path_reaches_approval_interrupt():
    services = make_services(llm=StubLLM(["PLAN: fix add", URLS, NOTES, OPS, APPROVED]),
                             sandbox=StubSandbox())  # default run result passes
    graph = build_graph(services, InMemorySaver())
    # Driven through RunManager so the pause path runs: the interrupted drive
    # sleeps the shared sandbox instance (C2 smoke).
    rm = RunManager(services, graph)
    await rm.drive("s1", initial())
    config = {"configurable": {"thread_id": "s1"}}
    state = (await graph.aget_state(config)).values
    assert state["code_diff"].startswith("diff --git")
    assert state["test_results"]["passed"] is True
    assert any(e.type == "cost_update" for e in services.publisher.events)
    assert services.sandbox_factory(initial()).slept is True


async def test_tester_failure_retries_coding_then_reaches_approval():
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED tests/test_calc.py::test_add", "")])
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, OPS2, APPROVED]), sandbox=sb)
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s2", initial())
    state = (await graph.aget_state(config)).values
    assert state["retry_counts"]["testing"] == 1
    assert state["test_results"]["passed"] is True  # second run passed
    assert sb.ensured >= 2  # same sandbox instance reused (warm VM story)


async def test_tester_bound_exceeded_reaches_needs_human():
    sb = StubSandbox(default_run_result=ExecResult(1, "FAILED x::y", ""))
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, OPS, OPS2, OPS2, OPS2]),
                             sandbox=sb)
    graph = build_graph(services, InMemorySaver())
    config = {"configurable": {"thread_id": "s3"}}
    async for _chunk in graph.astream(initial(), config, stream_mode="updates"):
        pass
    state = (await graph.aget_state(config)).values
    assert state["status"] == "needs_human"
    assert state["retry_counts"]["testing"] == 3
    coding_llm_calls = sum(1 for c in services.llm.calls if c["model"].endswith("claude-sonnet-4.5"))
    assert coding_llm_calls == 4  # initial + 3 retries


async def test_reviewer_rejection_retries_coding():
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS,
                                          NEEDS_CHANGES, OPS2, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s4", initial())
    state = (await graph.aget_state(config)).values
    assert state["retry_counts"]["coding"] == 1
    assert state["test_results"]["passed"] is True


async def test_resume_approved_completes_done():
    services = make_services(llm=StubLLM(["PLAN: fix add", URLS, NOTES, OPS, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s5", initial())
    async for _chunk in graph.astream(Command(resume={"decision": "approved", "feedback": ""}),
                                      config, stream_mode="updates"):
        pass
    state = (await graph.aget_state(config)).values
    assert state["status"] == "done"
    assert state["approval_status"] == "approved"


async def test_resume_rejected_loops_to_planner_with_feedback():
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, APPROVED,
                                          "REVISED PLAN", URLS, NOTES, OPS2, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s6", initial())
    async for _chunk in graph.astream(Command(resume={"decision": "rejected",
                                                      "feedback": "use uuid"}),
                                      config, stream_mode="updates"):
        if "__interrupt__" in _chunk:
            break
    state = (await graph.aget_state(config)).values
    assert state["task_description"].endswith("HUMAN FEEDBACK: use uuid")
    assert state["plan"] == "REVISED PLAN"  # planner re-entered
