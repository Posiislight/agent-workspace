from app.graph.nodes.helpers import conversation_id, emit

PLANNER_PROMPT = """You are the Planner agent in an automated engineering workflow. You do not write
code. Given a task description and read-only access to the target repository,
produce a concrete, ordered implementation plan: which files will be created or
modified, what the change in each does, and any sequencing constraints between
steps. If the task is ambiguous, state your interpretation explicitly rather than
asking a question - a human will review your plan before code is written. If you
are re-entering after review or human feedback, revise the previous plan to
address every point raised; do not silently drop unaddressed feedback."""


async def planner_node(state, *, services):
    await emit(services, state, "planner", "node_started", {})
    await emit(services, state, "planner", "tool_call", {"action": "read_file_tree"})
    tree = await _file_tree(state, services)
    prior = ""
    if state.get("plan") and state.get("approval_status") == "rejected":
        prior = (f"\n\nPREVIOUS PLAN (revise it, addressing all human feedback; "
                 f"do not silently drop any point):\n{state['plan']}")
    prompt = (PLANNER_PROMPT +
              f"\n\nRepository: {state['repo']} (base branch {state['base_branch']})\n"
              f"File tree (depth 2):\n{tree}\n\nTask: {state['task_description']}{prior}")
    cid = conversation_id(state["task_id"], "planner",
                          state["retry_counts"].get("coding", 0))
    plan = await services.agent.chat(state["agent_id"], prompt, cid)
    updates = {"plan": plan, "status": "researching", "error_log": list(state["error_log"])}
    await emit(services, state, "planner", "node_completed", {"plan_chars": len(plan)})
    return updates


async def _file_tree(state, services) -> str:
    try:
        sb = services.sandbox_factory(state)
        await sb.ensure()
        res = await sb.exec(
            "cd /data/workspace && find . -maxdepth 2 -not -path '*/.git*' | head -200")
        return res.stdout or "(empty)"
    except Exception as e:  # noqa: BLE001 - planner must not crash on repo read failure
        return f"(repo read failed: {e})"