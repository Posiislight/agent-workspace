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
    if state.get("vm_seconds"):
        lines.append(f"- VM awake: {state['vm_seconds']:.0f}s "
                     f"(LLM ${state.get('llm_cost', 0.0):.4f} + VM ${state.get('vm_cost', 0.0):.4f})")
    if state.get("budget_usd"):
        lines.append(f"- Budget: ${state['budget_usd']:.2f}")
    harness = state.get("harness")
    if harness:
        lines.append(f"- Harness: `{harness}`")
    metrics = state.get("workspace_metrics") or []
    if metrics:
        lines.extend(["", "## Workspace (persistent repo agent)",
                      "| start | agent | deps | ready (s) |", "|---|---|---|---|"])
        for m in metrics[-5:]:
            lines.append(f"| {'cold' if m.get('cold') else 'warm'} | `{m.get('agent')}` | "
                         f"{m.get('deps')} | {m.get('ready_seconds')} |")
    cands = state.get("candidate_results") or []
    if cands:
        lines.extend(["", "## Best-of-N scoreboard",
                      "| # | harness | tests | +/- | files | time (s) | score | |",
                      "|---|---|---|---|---|---|---|---|"])
        for c in cands:
            lines.append(
                f"| {c.get('index')} | `{c.get('harness')}` | "
                f"{'pass' if c.get('tests_passed') else 'fail'} | "
                f"+{c.get('insertions', 0)}/-{c.get('deletions', 0)} | {c.get('files', 0)} | "
                f"{c.get('seconds', 0)} | {c.get('score', '')} | "
                f"{'**winner**' if c.get('winner') else ''} |")
    proof = state.get("visual_proof") or {}
    if proof.get("github"):
        g = proof["github"]
        lines.extend(["", "## Visual proof", "| before | after |", "|---|---|",
                      f"| ![before]({g.get('before')}) | ![after]({g.get('after')}) |"])
    followups = state.get("followups") or []
    if followups:
        lines.extend(["", "## Follow-ups"])
        for f in followups:
            lines.append(f"- _{f.get('source')}_: {(f.get('instruction') or '')[:200]}")
    diff = state.get("code_diff")
    if diff:
        lines.extend(["", "## Diff", "```diff", diff[:50000], "```"])
    paused = state.get("paused_at")
    if paused:
        lines.append(f"- Paused at: {paused}")
    return "\n".join(lines)


async def publish_pr(services, state) -> dict:
    """Push the current revision and open or refresh the draft PR (every round)."""
    repo = state["repo"]
    branch = branch_for(state["task_id"])
    await push_branch(services.sandbox_factory(state), repo, branch,
                      services.settings.github_pat)
    existing = await services.github.find_open_pr(repo, f"{repo.split('/')[0]}:{branch}")
    if existing:
        try:
            await services.github.update_pr(repo, existing["number"], body=render_pr_body(state))
        except Exception:  # noqa: BLE001, S110 - a stale body must not block the gate
            pass
        return {"number": existing["number"], "html_url": existing["html_url"],
                "draft": existing.get("draft", True)}
    pr = await services.github.create_pr(
        repo, title=f"aw: {state['task_description'][:60]}",
        body=render_pr_body(state), head=branch, base=state["base_branch"], draft=True)
    return {"number": pr["number"], "html_url": pr["html_url"], "draft": pr.get("draft", True)}


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