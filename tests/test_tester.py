from app.sandbox.base import ExecResult
from tests.fakes import StubSandbox, make_services


def _tester_state(**kw):
    st = {"task_id": "t1", "test_command": "pytest -q", "error_log": [],
          "retry_counts": {"testing": 0, "coding": 0}, "repo": "org/repo",
          "base_branch": "main", "sandbox_id": None}
    st.update(kw)
    return st


async def test_pass_routes_to_reviewer():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(0, "1 passed", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(_tester_state(), services=s)
    assert res.update["test_results"]["passed"] is True
    assert res.goto == "reviewer"
    assert sb.run_calls == ["pytest -q"]


async def test_failure_parses_failing_tests_and_routes_to_coding():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED tests/test_calc.py::test_add - assert", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(_tester_state(), services=s)
    assert res.update["test_results"]["failing_tests"] == ["tests/test_calc.py::test_add"]
    assert res.goto == "coding_agent"


async def test_bound_exceeded_routes_to_needs_human():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED x::y", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(_tester_state(retry_counts={"testing": 3, "coding": 0}), services=s)
    assert res.goto == "needs_human"
    assert res.update["status"] == "needs_human"
    assert "retry bound exceeded" in res.update["error_log"][-1]


async def test_syncs_deps_before_running_tests():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(0, "1 passed", "")])
    s = make_services(sandbox=sb)
    await tester_node(_tester_state(), services=s)
    assert sb.deps_synced == 1
    assert sb.run_calls == ["pytest -q"]
