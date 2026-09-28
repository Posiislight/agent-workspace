import re

from langgraph.types import Command

from app.graph.edges import route_after_tester
from app.graph.nodes.helpers import apply_vm_cost, budget_stop, check_budget, emit
from app.graph.state import TestResult


async def tester_node(state, *, services):
    """Mechanical node — no LLM. Runs the suite, reports structured results (spec §6)."""
    await emit(services, state, "tester", "node_started", {})
    budget, stop = await check_budget(state, services, "tester")
    if stop:
        return budget_stop(state, "tester")
    state = {**state, **budget}
    sb = services.sandbox_factory(state)
    await sb.ensure()
    cmd = state.get("test_command") or services.settings.test_command
    res = await sb.run_long(cmd, timeout=services.settings.sandbox_run_timeout_seconds)
    await emit(services, state, "tester", "tool_call", {"command": cmd, "exit_code": res.exit_code})
    failing = sorted(set(re.findall(r"^FAILED\s+(\S+)", res.combined, re.M)))
    tr = TestResult(passed=res.exit_code == 0, failing_output=res.combined[-8000:],
                    failing_tests=failing).to_dict()
    await emit(services, state, "tester", "node_completed", {"passed": tr["passed"]})
    base = await apply_vm_cost({"test_results": tr, **budget}, state, services, "tester",
                               sandbox=sb)
    route = route_after_tester({**state, "test_results": tr})
    if route == "needs_human":
        err = list(state["error_log"]) + [
            f"tester: retry bound exceeded (exit_code={res.exit_code}); "
            f"last failure: {tr['failing_output'][:500]}"]
        return Command(update={**base, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update={**base,
                           "status": "reviewing" if route == "reviewer" else "coding"},
                   goto=route)
