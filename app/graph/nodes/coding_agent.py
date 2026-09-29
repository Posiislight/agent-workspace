import re

from langgraph.types import Command

from app.graph.nodes.helpers import conversation_id, emit

CODING_PROMPT = """You are the Coding Agent working inside a VM that already has the
target repository cloned at /data/workspace (base branch {base}). Implement the task
below directly in that checkout using your own tools: edit files and run commands
yourself. Do NOT git commit or push; leave the finished change as uncommitted
modifications.

There is no separate planner or researcher: first explore the repo yourself
(structure, conventions, the libraries and versions it uses, similar code
elsewhere) and decide on a concrete plan before editing. If the task is
ambiguous, state your interpretation rather than asking.

TESTS: add or update tests for every behaviour you change, using the repo's
existing test framework and layout. If the repo has no test suite, create one
(pytest for Python, under tests/) with at least one test of the new logic,
e.g. the edge cases the plan mentions such as an action with no selection or
empty input. Keep tests runnable headlessly (no real GUI/network); factor logic
out of UI callbacks or mock the UI toolkit if needed. Run the tests yourself
before finishing.

When you believe the work is complete, reply with a short PLAN summary (which
files you changed and why, and any interpretation you made), then DONE on its
own line. Otherwise describe what blocked you. If you disagree with a review
comment, still reply DONE but explain why you left it unchanged.

{context}
TASK:
{task}
{previous}"""

REPLY_EXCERPT_CHARS = 2000
_DONE = re.compile(r"^\W*DONE\W*$", re.M)


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

    previous = ""
    if state.get("plan"):
        previous = ("\nYOUR PREVIOUS PLAN (the checkout still holds that change; revise "
                    "it rather than starting over):\n" + state["plan"])
    prompt = CODING_PROMPT.format(
        base=state["base_branch"], context=context,
        task=state["task_description"], previous=previous)
    retry = updates["retry_counts"].get("coding", 0)
    cid = conversation_id(state["task_id"], "coding", retry)
    reply = await services.agent.chat(state["agent_id"], prompt, cid) or ""
    excerpt = reply.strip()[-REPLY_EXCERPT_CHARS:]

    if not _DONE.search(reply):
        updates["error_log"] = updates["error_log"] + [
            f"coding_agent: reply did not contain DONE; reply: {excerpt}"]
        await emit(services, state, "coding_agent", "node_completed",
                   {"done": False, "reply": excerpt})
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")

    plan = _DONE.sub("", reply).strip()
    if plan:
        updates["plan"] = plan

    diff = await sb.diff()
    if not diff.strip():
        updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    updates["code_diff"] = diff
    updates["status"] = "testing"
    await emit(services, state, "coding_agent", "node_completed",
               {"done": True, "diff_chars": len(diff), "reply": excerpt})
    return Command(update=updates, goto="tester")
