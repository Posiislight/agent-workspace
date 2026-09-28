import json
import re

from langgraph.types import Command

from app.graph.edges import route_after_reviewer
from app.graph.nodes.helpers import conversation_id, emit

REVIEWER_PROMPT = """You are the Reviewer agent. You review a code diff that has already passed
tests. Check that it actually satisfies the original task (not just "tests
pass"), follows the codebase's existing conventions, and doesn't introduce
obvious risk (unhandled errors, missing edge cases, security issues). If you
request changes, be specific enough that the Coding Agent can act without
further clarification. You are a gate before human review, not a replacement
for it - when in doubt, approve and let the human decide, rather than looping
indefinitely.

Reply with ONLY: {"verdict": "approved" | "needs_changes", "comments": ["..."]}"""


async def reviewer_node(state, *, services):
    await emit(services, state, "reviewer", "node_started", {})
    prompt = (REVIEWER_PROMPT +
              f"\n\nORIGINAL TASK:\n{state['task_description']}\n\nPLAN:\n{state.get('plan') or ''}\n\n"
              f"TEST RESULTS:\n{json.dumps(state.get('test_results'))}\n\nDIFF:\n{state.get('code_diff') or ''}")
    cid = conversation_id(state["task_id"], "reviewer",
                          state["retry_counts"].get("coding", 0))
    result = await services.agent.chat(state["agent_id"], prompt, cid)
    verdict, comments = _parse_verdict(result)
    updates = {"status": "reviewing",
               "approval_status": "approved" if verdict == "approved" else "needs_changes",
               "review_comments": None if verdict == "approved" else comments}
    await emit(services, state, "reviewer", "node_completed", {"verdict": verdict})
    route = route_after_reviewer({**state, "approval_status": updates["approval_status"]})
    if route == "needs_human":
        err = list(state["error_log"]) + [f"reviewer: retry bound exceeded; last comments={comments[:5]}"]
        return Command(update={**updates, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update=updates, goto=route)


def _parse_verdict(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("verdict") in ("approved", "needs_changes"):
                return d["verdict"], [str(c) for c in d.get("comments", [])]
        except json.JSONDecodeError:
            pass
    return "needs_changes", ["reviewer returned unparseable output", text[:500]]
