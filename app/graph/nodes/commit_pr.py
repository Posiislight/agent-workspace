from app.github.pr_flow import ensure_draft_pr, render_final_body
from app.github.push import branch_for, push_branch
from app.graph.nodes.helpers import emit


async def _totals(services, state) -> dict:
    tracker = getattr(services, "cost", None)
    if tracker is None or services.redis is None:
        llm = state.get("cost_so_far", 0.0)
        return {"llm_cost": llm, "vm_cost": 0.0, "vm_minutes": 0.0,
                "total": round(llm, 6)}
    snap = await tracker.snapshot(state["task_id"])
    return {**snap, "total": round(snap["llm_cost"] + snap["vm_cost"], 6)}


async def commit_pr_node(state, *, services):
    """Finalize (reachable only from an approved HumanApproval): push, mark
    ready (or merge per config), write final totals into the PR description,
    append the summary comment, mark done."""
    await emit(services, state, "commit_pr", "node_started", {})
    github = services.github
    repo = state["repo"]
    branch = branch_for(state["task_id"])
    await push_branch(services.sandbox_factory(state), repo, branch,
                      services.settings.github_pat)
    if state.get("pr_number"):
        pr = {"number": state["pr_number"], "html_url": state["pr_url"],
              "draft": True}
    else:
        pr = await ensure_draft_pr(services, state)
    totals = await _totals(services, state)
    await github.update_pr_body(repo, pr["number"],
                                render_final_body(state, totals))
    if services.settings.merge_pr_when_ready:
        await github.merge_pr(repo, pr["number"])
    else:
        await github.mark_ready(repo, pr["number"])
    retries = state.get("retry_counts") or {}
    summary = "\n".join([
        "## Final summary",
        f"- Total cost: ${totals['total']:.2f}",
        (f"- LLM: ${totals['llm_cost']:.2f} · VM: "
         f"${totals['vm_cost']:.2f} ({totals['vm_minutes']:.1f} awake min)"),
        (f"- Retry cycles: testing: {retries.get('testing', 0)}, "
         f"coding: {retries.get('coding', 0)}"),
    ])
    if state.get("paused_at") and state.get("resumed_at"):
        summary += f"\n- Paused at: {state['paused_at']}, resumed at: {state['resumed_at']}"
    comments = await github.list_comments(repo, pr["number"])
    already_posted = any(c.get("body", "").startswith("## Final summary")
                         for c in comments)
    if not already_posted:
        await github.add_comment(repo, pr["number"], summary)
    await emit(services, state, "commit_pr", "node_completed",
               {"pr_url": pr["html_url"]})
    return {"status": "done", "pr_url": pr["html_url"]}
