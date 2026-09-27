from app.github.push import branch_for, push_branch


def render_pr_body(state: dict) -> str:
    retries = state.get("retry_counts") or {}
    lines = [
        "## Task",
        state.get("task_description", ""),
        "",
        "## Plan",
        state.get("plan") or "(no plan captured)",
        "",
        "## Test results",
        str(state.get("test_results") or "(none)"),
        "",
        "## Reviewer comments",
        "\n".join(state.get("review_comments") or []) or "(none)",
        "",
        "## Run stats",
        f"- Running cost: ${state.get('cost_so_far', 0.0):.2f}",
        ("- Retry cycles: testing: "
         f"{retries.get('testing', 0)}, coding: {retries.get('coding', 0)}"),
    ]
    diff = state.get("code_diff")
    if diff:
        lines.extend(["", "## Diff", diff])
    paused = state.get("paused_at")
    if paused:
        lines.append(f"- Paused at: {paused}")
    return "\n".join(lines)


def render_final_body(state: dict, totals: dict) -> str:
    body = render_pr_body({**state, "cost_so_far": totals.get("total", 0.0)})
    return "\n".join([
        body, "",
        "## Final totals",
        f"- LLM: ${totals.get('llm_cost', 0.0):.2f}",
        f"- VM: ${totals.get('vm_cost', 0.0):.2f} "
        f"({totals.get('vm_minutes', 0.0):.1f} min awake)",
        f"- Total: ${totals.get('total', 0.0):.2f}",
    ])


async def ensure_draft_pr(services, state) -> dict:
    repo = state["repo"]
    branch = branch_for(state["task_id"])
    head = f"{repo.split('/')[0]}:{branch}"
    existing = await services.github.find_open_pr(repo, head)
    if existing:
        return {"number": existing["number"], "html_url": existing["html_url"],
                "draft": existing.get("draft", True)}
    await push_branch(services.sandbox_factory(state), repo, branch,
                      services.settings.github_pat)
    pr = await services.github.create_pr(
        repo, title=f"aw: {state['task_description'][:60]}",
        body=render_pr_body(state), head=branch, base=state["base_branch"],
        draft=True)
    return {"number": pr["number"], "html_url": pr["html_url"],
            "draft": pr.get("draft", True)}