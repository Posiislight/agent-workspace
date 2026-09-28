from langgraph.types import Command

from app.graph.nodes.helpers import conversation_id, emit

CODING_PROMPT = """You are the Coding Agent working inside a VM that already has the
target repository cloned at /data/workspace (base branch {base}). Implement the task
below directly in that checkout using your own tools: edit files and run commands
yourself. Do NOT git commit or push; leave the finished change as uncommitted
modifications. When you believe the work is complete, reply DONE on its own line,
otherwise describe what blocked you.

{context}
TASK:
{task}

PLAN:
{plan}

RESEARCH NOTES:
{notes}"""


async def coding_agent_node(state, *, services):
    await emit(services, state, "coding_agent", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()

    updates = {"status": "coding", "error_log": list(state["error_log"]),
               "retry_counts": dict(state["retry_counts"])}
    context = ""
    if state.get("review_comments"):
        context = "REVIEW COMMENTS TO ADDRESS:\n" + "\n".join(state["review_comments"])
        updates["review_comments"] = None
        updates["retry_counts"] = {**state["retry_counts"],
                                   "coding": state["retry_counts"].get("coding", 0) + 1}
    elif state.get("test_results") and not state["test_results"]["passed"]:
        context = ("TEST FAILURE DETAILS:\n"
                   + (state["test_results"].get("failing_output") or ""))
        updates["retry_counts"] = {**state["retry_counts"],
                                   "testing": state["retry_counts"].get("testing", 0) + 1}

    prompt = CODING_PROMPT.format(
        base=state["base_branch"], context=context,
        task=state["task_description"], plan=state.get("plan") or "(none)",
        notes=state.get("research_notes") or "(none)")
    retry = updates["retry_counts"].get("coding", 0)
    cid = conversation_id(state["task_id"], "coding", retry)
    await services.agent.chat(state["agent_id"], prompt, cid)

    diff = await sb.diff()
    if not diff.strip():
        updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    updates["code_diff"] = diff
    updates["status"] = "testing"
    await emit(services, state, "coding_agent", "node_completed", {"diff_chars": len(diff)})
    return Command(update=updates, goto="tester")
