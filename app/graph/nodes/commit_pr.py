import re

from app.github.pr_flow import ensure_draft_pr
from app.github.push import branch_for, push_branch
from app.graph.nodes.helpers import emit, now_iso


async def commit_pr_node(state, *, services):
    """Finalize (reachable only from an approved HumanApproval): push, mark
    ready (or merge per config), append the final summary, mark done."""
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
    if services.settings.merge_pr_when_ready:
        await github.merge_pr(repo, pr["number"])
    else:
        await github.mark_ready(repo, pr["number"])
    retries = state.get("retry_counts") or {}
    summary = "\n".join([
        "## Final summary",
        f"- Total cost: ${state.get('cost_so_far', 0.0):.2f}",
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
    await _remember(services, state)
    await emit(services, state, "commit_pr", "node_completed",
               {"pr_url": pr["html_url"]})
    return {"status": "done", "pr_url": pr["html_url"]}


async def _remember(services, state):
    """Append a line to the repo agent's memory (spec phase-4 §6)."""
    sb = services.sandbox_factory(state)
    append = getattr(sb, "append_memory", None)
    if append is None:
        return
    files = sorted(set(re.findall(r"^\+\+\+ b/(\S+)", state.get("code_diff") or "", re.MULTILINE)))
    try:
        await append(f"- {now_iso()[:10]} [{state.get('harness') or 'openrouter'}] "
                     f"{state['task_description'][:160]} | files: {', '.join(files[:8]) or '-'}"
                     f" | tests: `{state.get('test_command') or services.settings.test_command}`"
                     f" | PR: {state.get('pr_url') or '-'}")
    except Exception:  # noqa: BLE001, S110 - memory is best-effort
        pass
