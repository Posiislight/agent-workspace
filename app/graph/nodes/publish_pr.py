from app.github.pr_flow import publish_pr
from app.graph.nodes.helpers import apply_vm_cost, emit


async def publish_pr_node(state, *, services):
    """Push this revision and open/refresh the draft PR before the approval gate.

    Runs on every lap (first pass, reject loops, follow-ups, CI fixes), so the PR
    always shows the code the human is being asked to approve.
    """
    await emit(services, state, "publish_pr", "node_started", {})
    pr = await publish_pr(services, state)
    await emit(services, state, "publish_pr", "tool_call",
               {"pr_url": pr["html_url"], "pr_number": pr["number"]})
    # Status stays as-is: "awaiting_approval" is only mirrored once the interrupt
    # has actually paused the drive (RunManager), so a resume can never race it.
    updates = {"pr_url": pr["html_url"], "pr_number": pr["number"]}
    updates = await apply_vm_cost(updates, state, services, "publish_pr")
    await emit(services, state, "publish_pr", "node_completed", {"pr_url": pr["html_url"]})
    return updates
