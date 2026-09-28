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
    deps = await sb.sync_deps()
    await emit(services, state, "tester", "tool_call",
               {"command": "sync_deps", "exit_code": deps.exit_code})
    if deps.exit_code != 0:
        # Don't run tests in a half-installed env; hand the install error to
        # the coding agent (usually a bad requirements file it wrote).
        exit_code = deps.exit_code
        tr = TestResult(passed=False,
                        failing_output="dependency install failed:\n" + deps.combined[-7900:],
                        failing_tests=[]).to_dict()
    else:
        res = await sb.run_long(cmd, timeout=services.settings.sandbox_run_timeout_seconds)
        exit_code = res.exit_code
        await emit(services, state, "tester", "tool_call", {"command": cmd, "exit_code": exit_code})
        failing = sorted(set(re.findall(r"^FAILED\s+(\S+)", res.combined, re.M)))
        output = res.combined[-8000:]
        # pytest exit 5 = no tests collected. Repos without a suite would
        # otherwise loop coding<->tester forever; pass and let the reviewer
        # see the note.
        no_tests = exit_code == 5
        if no_tests:
            output = "no tests collected (pytest exit 5); change is unverified by tests\n" + output
        tr = TestResult(passed=exit_code == 0 or no_tests, failing_output=output,
                        failing_tests=failing).to_dict()
    await emit(services, state, "tester", "node_completed", {"passed": tr["passed"]})
    route = route_after_tester({**state, "test_results": tr})
    if route == "needs_human":
        err = list(state["error_log"]) + [
            f"tester: retry bound exceeded (exit_code={exit_code}); "
            f"last failure: {tr['failing_output'][:500]}"]
        return Command(update={"test_results": tr, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update={"test_results": tr,
                           "status": "reviewing" if route == "reviewer" else "coding"},
                   goto=route)
