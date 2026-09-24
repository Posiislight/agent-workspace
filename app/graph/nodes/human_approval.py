from inspect import isawaitable

from langgraph.types import Command, interrupt

from app.graph.nodes.helpers import emit


async def human_approval_node(state, *, services):
    """LangGraph interrupt() gate — pauses until resumed with a decision payload.

    On resume the node re-runs from the top; the only pre-interrupt side effect
    is the node_started emit (idempotent). The pause/resume timestamps are
    managed by RunManager (Task 13), not here.
    """
    await emit(services, state, "human_approval", "node_started", {})
    payload = interrupt({
        "task_id": state["task_id"],
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
    if decision == "approved":
        return Command(update={"approval_status": "approved", "status": "committing"},
                       goto="commit_pr")
    desc = state["task_description"] + (f"\n\nHUMAN FEEDBACK: {feedback}" if feedback else "")
    return Command(update={"approval_status": "rejected", "task_description": desc,
                           "status": "planning"},
                   goto="planner")
