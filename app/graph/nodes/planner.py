from app.graph.nodes.helpers import apply_llm_cost, chat_with_retry, emit

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
    memory = await _repo_memory(state, services)
    prior = ""
    if state.get("plan") and state.get("approval_status") == "rejected":
        prior = (f"\n\nPREVIOUS PLAN (revise it, addressing all human feedback; "
                 f"do not silently drop any point):\n{state['plan']}")
    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content":
            f"Repository: {state['repo']} (base branch {state['base_branch']})\n"
            f"File tree (depth 2):\n{tree}\n\n"
            + (f"REPO MEMORY (what earlier tasks on this repo did - follow the same "
               f"conventions):\n{memory}\n\n" if memory else "")
            + f"Task: {state['task_description']}{prior}"},
    ]
    result = await chat_with_retry(services, state, "planner", messages)
    updates = {"plan": result.text, "status": "researching", "error_log": list(state["error_log"])}
    updates = await apply_llm_cost(updates, state, services, result, "planner")
    await emit(services, state, "planner", "node_completed", {"plan_chars": len(result.text)})
    return updates


async def _repo_memory(state, services) -> str:
    try:
        sb = services.sandbox_factory(state)
        read = getattr(sb, "read_memory", None)
        return (await read()) if read else ""
    except Exception:  # noqa: BLE001 - memory is a hint, never a blocker
        return ""


async def _file_tree(state, services) -> str:
    try:
        sb = services.sandbox_factory(state)
        await sb.ensure()
        res = await sb.exec(
            "cd /data/workspace && find . -maxdepth 2 -not -path '*/.git*' | head -200")
        return res.stdout or "(empty)"
    except Exception as e:  # noqa: BLE001 - planner must not crash on repo read failure
        return f"(repo read failed: {e})"