import json

from tests.fakes import make_services

OPS_DONE = json.dumps({"ops": [{"op": "write_file", "path": "calc.py",
                                "content": "def add(a, b):\n    return a + b\n"}], "done": True})
OPS_FIX = json.dumps({"ops": [{"op": "shell", "command": "echo fixing"}], "done": True})


def coding_state(**kw):
    st = {"task_id": "t1", "task_description": "fix add", "repo": "org/repo", "base_branch": "main",
          "plan": "PLAN", "research_notes": "notes", "model_overrides": {}, "cost_so_far": 0.0,
          "error_log": [], "retry_counts": {"testing": 0, "coding": 0},
          "test_results": None, "review_comments": None}
    st.update(kw)
    return st


async def test_first_entry_writes_files_and_captures_diff():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_DONE])
    s = make_services(llm=llm)
    res = await coding_agent_node(coding_state(), services=s)
    assert res.update["code_diff"].startswith("diff --git")
    assert res.update["status"] == "testing"
    assert res.update["retry_counts"] == {"testing": 0, "coding": 0}
    assert res.goto == "tester"
    sb = s.sandbox_factory({})
    assert ("/data/workspace/calc.py", "def add(a, b):\n    return a + b\n") in sb.written
    tool = [e for e in s.publisher.events if e.type == "tool_call"]
    assert any(e.data.get("op") == "write_file" for e in tool)


async def test_retry_after_test_failure_increments_testing_counter():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_FIX])
    s = make_services(llm=llm)
    st = coding_state(test_results={"passed": False, "failing_output": "E: assert 2+2==5",
                                    "failing_tests": ["tests/test_calc.py::test_add"]})
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["testing"] == 1
    assert res.update["retry_counts"]["coding"] == 0
    sent = llm.calls[0]["messages"][-1]["content"]
    assert "E: assert 2+2==5" in sent


async def test_retry_after_review_consumes_comments():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_FIX])
    s = make_services(llm=llm)
    st = coding_state(review_comments=["use int not str"])
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["coding"] == 1
    assert res.update["retry_counts"]["testing"] == 0
    assert res.update["review_comments"] is None
    assert "use int not str" in llm.calls[0]["messages"][-1]["content"]
