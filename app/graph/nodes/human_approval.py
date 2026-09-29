from inspect import isawaitable

from langgraph.types import Command, interrupt

from app.github.pr_flow import ensure_draft_pr
from app.graph.nodes.helpers import emit


async def human_approval_node(state, *, services):
    """LangGraph interrupt() gate — pauses until resumed with a decision payload.

    Pre-interrupt side effect: push the branch and open a draft PR. This node
    re-runs from the top on resume, so ensure_draft_pr is idempotent (it returns
    the existing open PR for the head branch instead of creating another one).
    """
    await emit(services, state, "human_approval", "node_started", {})
    pr = await ensure_draft_pr(services, state)
    await emit(services, state, "human_approval", "tool_call",
               {"pr_url": pr["html_url"], "pr_number": pr["number"]})
    payload = interrupt({
        "task_id": state["task_id"],
        "pr": pr,
        "summary": {"plan_chars": len(state.get("plan") or ""),
                    "diff_chars": len(state.get("code_diff") or ""),
                    "test_results": state.get("test_results"),
                    "review_comments": state.get("review_comments"),
                    "cost_so_far": state.get("cost_so_far")},
        "instructions": "Resume with {'decision': 'approved' | 'rejected', 'feedback': '...'}",
    })
    if isawaitable(payload):
        payload = await payload
    decision = payload.get("decision")
    feedback = payload.get("feedback") or ""
    pr_update = {"pr_url": pr["html_url"], "pr_number": pr["number"]}
    if decision == "approved":
        return Command(update={"approval_status": "approved",
                               "status": "committing", **pr_update},
                       goto="commit_pr")
    desc = state["task_description"] + (f"\n\nHUMAN FEEDBACK: {feedback}" if feedback else "")
    return Command(update={"approval_status": "rejected", "task_description": desc,
                           "status": "coding", **pr_update},
                   goto="coding_agent")
