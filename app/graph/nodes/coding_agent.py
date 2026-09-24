import json
import re

from langgraph.types import Command

from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

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
    await emit(services, state, "coding_agent", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()

    updates = {"status": "coding", "error_log": list(state["error_log"]),
               "retry_counts": dict(state["retry_counts"])}
    context = ""
    if state.get("review_comments"):
        context = "REVIEW COMMENTS TO ADDRESS:\n" + "\n".join(state["review_comments"])
        updates["review_comments"] = None  # consumed
        updates["retry_counts"] = {**state["retry_counts"],
                                   "coding": state["retry_counts"].get("coding", 0) + 1}
    elif state.get("test_results") and not state["test_results"]["passed"]:
        context = ("TEST FAILURE DETAILS:\n"
                   + (state["test_results"].get("failing_output") or ""))
        updates["retry_counts"] = {**state["retry_counts"],
                                   "testing": state["retry_counts"].get("testing", 0) + 1}

    model = model_for(services, state, "coding_agent")
    messages = [{"role": "system", "content": CODING_AGENT_PROMPT},
                {"role": "user", "content": _brief(state, context)}]

    bad_rounds = 0
    for _round in range(MAX_ROUNDS):
        result = await services.llm.chat(model, messages)
        try:
            ops, done = _parse_round(result.text)
        except (ValueError, json.JSONDecodeError) as e:
            bad_rounds += 1
            if bad_rounds >= 2:
                updates["error_log"] = updates["error_log"] + [f"coding_agent: unparseable rounds ({e})"]
                return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content":
                          f"Invalid response ({e}). Reply with ONLY the JSON object."}]
            continue
        try:
            for op in ops:
                await _apply_op(services, state, sb, op)
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            bad_rounds += 1
            if bad_rounds >= 2:
                updates["error_log"] = updates["error_log"] + [f"coding_agent: malformed ops ({e})"]
                return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content":
                          f"Ops failed to apply ({e}). Reply with ONLY the JSON object with valid ops."}]
            continue
        bad_rounds = 0
        if done:
            diff = await sb.diff()
            if not diff.strip():
                updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
                return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
            updates["code_diff"] = diff
            updates["status"] = "testing"
            updates = await apply_llm_cost(updates, {**state, **updates}, services,
                                           result, "coding_agent")
            await emit(services, state, "coding_agent", "node_completed", {"diff_chars": len(diff)})
            return Command(update=updates, goto="tester")
        messages += [{"role": "assistant", "content": result.text},
                     {"role": "user", "content": "Round executed. Continue."}]

    updates["error_log"] = updates["error_log"] + [f"coding_agent: no convergence in {MAX_ROUNDS} rounds"]
    return Command(update={**updates, "status": "needs_human"}, goto="needs_human")


def _parse_round(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object found in response")
    d = json.loads(m.group(0))
    if not isinstance(d.get("ops"), list):
        raise ValueError("missing ops list")
    return d["ops"], bool(d.get("done"))


async def _apply_op(services, state, sb, op):
    kind = op.get("op")
    if kind == "write_file":
        path = "/data/workspace/" + str(op["path"]).lstrip("/")
        await sb.write_file(path, str(op["content"]))
        await emit(services, state, "coding_agent", "tool_call",
                   {"op": "write_file", "path": op["path"], "chars": len(str(op["content"]))})
    elif kind == "shell":
        res = await sb.exec(str(op["command"]))
        await emit(services, state, "coding_agent", "tool_call",
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
