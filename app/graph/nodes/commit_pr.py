from app.graph.nodes.helpers import emit


async def commit_pr_node(state, *, services):
    """Phase-1 finalize: mark done locally. Push/PR is the phase-2 plan."""
    await emit(services, state, "commit_pr", "node_started", {})
    try:
        sb = services.sandbox_factory(state)
        stat = await sb.exec("cd /data/workspace && git diff --cached --stat")
        summary = stat.stdout[:1000]
    except Exception as e:  # noqa: BLE001 - finalize must not crash
        summary = f"(stat unavailable: {e})"
    await emit(services, state, "commit_pr", "node_completed", {"summary": summary})
    return {"status": "done", "pr_url": None}
