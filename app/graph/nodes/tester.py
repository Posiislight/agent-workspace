import re

from langgraph.types import Command

from app.graph.edges import route_after_tester
from app.graph.nodes.helpers import emit
from app.graph.state import TestResult


async def tester_node(state, *, services):
    """Mechanical node — no LLM. Runs the suite, reports structured results (spec §6)."""
    await emit(services, state, "tester", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()
    cmd = state.get("test_command") or services.settings.test_command
    res = await sb.run_long(cmd, timeout=services.settings.sandbox_run_timeout_seconds)
    await emit(services, state, "tester", "tool_call", {"command": cmd, "exit_code": res.exit_code})
    failing = sorted(set(re.findall(r"^FAILED\s+(\S+)", res.combined, re.M)))
    tr = TestResult(passed=res.exit_code == 0, failing_output=res.combined[-8000:],
                    failing_tests=failing).to_dict()
    await emit(services, state, "tester", "node_completed", {"passed": tr["passed"]})
    route = route_after_tester({**state, "test_results": tr})
    if route == "needs_human":
        err = list(state["error_log"]) + [
            f"tester: retry bound exceeded (exit_code={res.exit_code}); "
            f"last failure: {tr['failing_output'][:500]}"]
        return Command(update={"test_results": tr, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update={"test_results": tr,
                           "status": "reviewing" if route == "reviewer" else "coding"},
                   goto=route)
