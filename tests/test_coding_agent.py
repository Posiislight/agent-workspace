from tests.fakes import StubAgent, StubSandbox, make_services


def coding_state(**kw):
    st = {"task_id": "t1", "task_description": "fix add", "repo": "org/repo", "base_branch": "main",
          "plan": "PLAN", "research_notes": "notes", "agent_id": "agent-1", "template_id": "codex",
          "error_log": [], "retry_counts": {"testing": 0, "coding": 0},
          "test_results": None, "review_comments": None}
    st.update(kw)
    return st


async def test_success_chats_once_and_routes_to_tester():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    res = await coding_agent_node(coding_state(), services=s)
    assert res.update["status"] == "testing"
    assert res.update["code_diff"].startswith("diff --git")
    assert res.update["retry_counts"] == {"testing": 0, "coding": 0}
    assert res.goto == "tester"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["conversation_id"] == "aw-t1-coding"
    assert call["agent_id"] == "agent-1"
    assert "TASK:\nfix add" in call["message"]
    assert "YOUR PREVIOUS PLAN (" in call["message"]
    assert call["message"].endswith("\nPLAN")


async def test_empty_diff_routes_to_needs_human():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox(diff_text=""))
    res = await coding_agent_node(coding_state(), services=s)
    assert res.update["status"] == "needs_human"
    assert res.goto == "needs_human"
    assert "empty diff" in res.update["error_log"][-1]
    assert len(stub.calls) == 1


async def test_retry_after_test_failure_increments_testing_counter():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    st = coding_state(test_results={"passed": False, "failing_output": "E: assert 2+2==5",
                                    "failing_tests": ["tests/test_calc.py::test_add"]})
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["testing"] == 1
    assert res.update["retry_counts"]["coding"] == 0
    assert "E: assert 2+2==5" in stub.calls[0]["message"]


async def test_retry_after_review_consumes_comments():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    st = coding_state(review_comments=["use int not str"])
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["coding"] == 1
    assert res.update["retry_counts"]["testing"] == 0
    assert res.update["review_comments"] is None
    assert "use int not str" in stub.calls[0]["message"]
    assert stub.calls[0]["conversation_id"] == "aw-t1-coding-r1"


async def test_prompt_asks_for_tests():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    await coding_agent_node(coding_state(), services=s)
    msg = stub.calls[0]["message"]
    assert "add or update tests" in msg
    assert "no test suite" in msg


async def test_reply_without_done_routes_to_needs_human():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["I could not install tkinter, so I stopped."])
    sb = StubSandbox()
    s = make_services(agent=stub, sandbox=sb)
    res = await coding_agent_node(coding_state(), services=s)
    assert res.goto == "needs_human"
    assert res.update["status"] == "needs_human"
    assert "did not contain DONE" in res.update["error_log"][-1]
    assert "could not install tkinter" in res.update["error_log"][-1]
    done = [e for e in s.publisher.events if e.type == "node_completed"][-1]
    assert done.data["done"] is False
    assert "could not install tkinter" in done.data["reply"]


async def test_done_detection_tolerates_markdown_and_explanation():
    from app.graph.nodes.coding_agent import coding_agent_node
    reply = "Removed root.bind; kept the listbox binding.\n\n**DONE**"
    stub = StubAgent(responses=[reply])
    s = make_services(agent=stub, sandbox=StubSandbox())
    res = await coding_agent_node(coding_state(), services=s)
    assert res.goto == "tester"
    done = [e for e in s.publisher.events if e.type == "node_completed"][-1]
    assert done.data["done"] is True
    assert "Removed root.bind" in done.data["reply"]


async def test_done_inside_a_word_is_not_done():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["Nothing was DONE yet, blocked on auth"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    res = await coding_agent_node(coding_state(), services=s)
    assert res.goto == "needs_human"


async def test_reply_excerpt_is_truncated():
    from app.graph.nodes.coding_agent import REPLY_EXCERPT_CHARS, coding_agent_node
    stub = StubAgent(responses=["x" * 10_000 + "\nDONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    await coding_agent_node(coding_state(), services=s)
    done = [e for e in s.publisher.events if e.type == "node_completed"][-1]
    assert len(done.data["reply"]) == REPLY_EXCERPT_CHARS
    assert done.data["reply"].endswith("DONE")


async def test_plan_is_taken_from_reply_without_done_line():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["PLAN: edit app.py to fix add\n\nDONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    res = await coding_agent_node(coding_state(plan=None), services=s)
    assert res.update["plan"] == "PLAN: edit app.py to fix add"
    assert "YOUR PREVIOUS PLAN" not in stub.calls[0]["message"]


async def test_bare_done_keeps_previous_plan():
    from app.graph.nodes.coding_agent import coding_agent_node
    stub = StubAgent(responses=["DONE"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    res = await coding_agent_node(coding_state(), services=s)
    assert "plan" not in res.update
