import json
import re
from dataclasses import dataclass, field

from langgraph.types import Command

from app.graph.nodes.helpers import (
    apply_llm_cost,
    apply_vm_cost,
    budget_stop,
    chat_with_retry,
    check_budget,
    emit,
)
from app.harness import CLI_HARNESSES, run_cli_harness

CODING_AGENT_PROMPT = """You are the Coding Agent. You implement the given plan, using the research
notes provided, inside an isolated sandbox with full read/write access to the
repository. Make the smallest correct change that satisfies the plan. If you
are re-entering because tests failed or a reviewer requested changes, the
failure details / review comments are included below - address them
specifically rather than rewriting unrelated code. When finished, leave the
change as a git diff against the base branch; do not push or open a PR
yourself.

Work in rounds. In every round reply with ONLY a JSON object:
{"ops": [{"op": "write_file", "path": "relative/path.py", "content": "..."},
         {"op": "shell", "command": "shell command"}], "done": true}
Set "done": true when the plan is fully implemented. Paths are relative to the
repository root. Never run git add, git commit, or git push yourself."""

MAX_ROUNDS = 8


async def coding_agent_node(state, *, services):
    harness = state.get("harness") or services.settings.default_harness
    await emit(services, state, "coding_agent", "node_started", {"harness": harness})
    budget, stop = await check_budget(state, services, "coding_agent")
    if stop:
        return budget_stop(state, "coding_agent")
    state = {**state, **budget}
    sb = services.sandbox_factory(state)
    await sb.ensure()

    updates = {"status": "coding", "error_log": list(state["error_log"]),
               "retry_counts": dict(state["retry_counts"]), **budget}
    context, consumed = build_context(state)
    updates.update(consumed)

    if harness in CLI_HARNESSES:
        return await _cli_round(state, services, sb, harness, context, updates)

    outcome = await openrouter_loop(state, services, sb, context)
    for result in outcome.results:
        updates = await apply_llm_cost(updates, {**state, **updates}, services,
                                       result, "coding_agent")
    if not outcome.ok:
        updates["error_log"] = updates["error_log"] + [f"coding_agent: {outcome.error}"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    diff = await sb.diff()
    if not diff.strip():
        updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    updates["code_diff"] = diff
    updates["status"] = "testing"
    await emit(services, state, "coding_agent", "node_completed", {"diff_chars": len(diff)})
    return Command(update=updates, goto="tester")


@dataclass
class LoopOutcome:
    ok: bool
    error: str = ""
    results: list = field(default_factory=list)  # every LLMResult, for cost


async def openrouter_loop(state, services, sb, context: str,
                          node: str = "coding_agent") -> LoopOutcome:
    """The in-process JSON-ops harness: rounds of LLM ops applied to the workspace."""
    messages = [{"role": "system", "content": CODING_AGENT_PROMPT},
                {"role": "user", "content": _brief(state, context)}]
    results = []
    bad_rounds = 0
    for _round in range(MAX_ROUNDS):
        result = await chat_with_retry(services, state, "coding_agent", messages)
        results.append(result)
        try:
            ops, done = _parse_round(result.text)
        except (ValueError, json.JSONDecodeError) as e:
            bad_rounds += 1
            if bad_rounds >= 2:
                return LoopOutcome(False, f"unparseable rounds ({e})", results)
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content":
                          f"Invalid response ({e}). Reply with ONLY the JSON object."}]
            continue
        try:
            for op in ops:
                await _apply_op(services, state, sb, op, node=node)
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            bad_rounds += 1
            if bad_rounds >= 2:
                return LoopOutcome(False, f"malformed ops ({e})", results)
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content":
                          f"Ops failed to apply ({e}). Reply with ONLY the JSON object with valid ops."}]
            continue
        bad_rounds = 0
        if done:
            return LoopOutcome(True, "", results)
        messages += [{"role": "assistant", "content": result.text},
                     {"role": "user", "content": "Round executed. Continue."}]
    return LoopOutcome(False, f"no convergence in {MAX_ROUNDS} rounds", results)


def build_context(state) -> tuple[str, dict]:
    """Returns (context for the brief, state updates consuming it)."""
    updates: dict = {}
    followup = state.get("followup_request")
    if followup:
        updates["followup_request"] = None  # consumed
        updates["review_comments"] = None
        return (f"FOLLOW-UP REQUEST (from {followup.get('source', 'chat')}). The branch "
                "already contains the earlier work for this task; apply this change on "
                "top of it and keep everything else intact:\n"
                + (followup.get("instruction") or "")), updates
    if state.get("review_comments"):
        updates["review_comments"] = None  # consumed
        updates["retry_counts"] = {**state["retry_counts"],
                                   "coding": state["retry_counts"].get("coding", 0) + 1}
        return "REVIEW COMMENTS TO ADDRESS:\n" + "\n".join(state["review_comments"]), updates
    if state.get("test_results") and not state["test_results"]["passed"]:
        updates["retry_counts"] = {**state["retry_counts"],
                                   "testing": state["retry_counts"].get("testing", 0) + 1}
        return ("TEST FAILURE DETAILS:\n"
                + (state["test_results"].get("failing_output") or "")), updates
    return "", updates


async def _cli_round(state, services, sb, harness, context, updates):
    await emit(services, state, "coding_agent", "tool_call",
               {"op": "harness_run", "harness": harness})
    res, _cmd = await run_cli_harness(services.settings, sb, harness, _brief(state, context),
                                     timeout=services.settings.sandbox_run_timeout_seconds)
    await emit(services, state, "coding_agent", "tool_call",
               {"op": "harness_done", "harness": harness, "exit_code": res.exit_code,
                "log_tail": res.combined[-1500:]})
    updates = await apply_vm_cost(updates, state, services, "coding_agent", sandbox=sb)
    if res.exit_code != 0:
        updates["error_log"] = updates["error_log"] + [
            f"coding_agent[{harness}]: harness exited {res.exit_code}: {res.combined[-800:]}"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    diff = await sb.diff()
    if not diff.strip():
        updates["error_log"] = updates["error_log"] + [f"coding_agent[{harness}]: empty diff"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    updates["code_diff"] = diff
    updates["status"] = "testing"
    await emit(services, state, "coding_agent", "node_completed",
               {"diff_chars": len(diff), "harness": harness})
    return Command(update=updates, goto="tester")


def _parse_round(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object found in response")
    d = json.loads(m.group(0))
    if not isinstance(d.get("ops"), list):
        raise ValueError("missing ops list")
    return d["ops"], bool(d.get("done"))


async def _apply_op(services, state, sb, op, node: str = "coding_agent"):
    kind = op.get("op")
    if kind == "write_file":
        path = "/data/workspace/" + str(op["path"]).lstrip("/")
        await sb.write_file(path, str(op["content"]))
        await emit(services, state, node, "tool_call",
                   {"op": "write_file", "path": op["path"], "chars": len(str(op["content"]))})
    elif kind == "shell":
        res = await sb.exec(str(op["command"]))
        await emit(services, state, node, "tool_call",
                   {"op": "shell", "command": op["command"], "exit_code": res.exit_code})
    else:
        raise ValueError(f"unknown op {kind!r}")


def _brief(state, context: str) -> str:
    parts = [f"Repository: {state['repo']} (base branch {state['base_branch']})",
             f"TASK:\n{state['task_description']}",
             f"PLAN:\n{state.get('plan') or '(none)'}",
             f"RESEARCH NOTES:\n{state.get('research_notes') or '(none)'}"]
    if context:
        parts.append(context)
    return "\n\n".join(parts)
