from tests.fakes import StubAgent, make_services


def rstate(**kw):
    st = {"task_id": "t1", "task_description": "fix add", "plan": "P", "code_diff": "diff --git",
          "test_results": {"passed": True, "failing_output": "", "failing_tests": []},
          "agent_id": "agent-1", "template_id": "codex", "error_log": [],
          "retry_counts": {"testing": 0, "coding": 0}}
    st.update(kw)
    return st


async def test_reviewer_approved_routes_to_human():
    from app.graph.nodes.reviewer import reviewer_node
    stub = StubAgent(responses=['{"verdict": "approved", "comments": []}'])
    s = make_services(agent=stub)
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "approved"
    assert res.update["review_comments"] is None
    assert res.goto == "human_approval"
    call = stub.calls[0]
    assert call["conversation_id"] == "aw-t1-reviewer"
    assert "DIFF:\ndiff --git" in call["message"]
    assert call["agent_id"] == "agent-1"


async def test_reviewer_needs_changes_routes_to_coding():
    from app.graph.nodes.reviewer import reviewer_node
    stub = StubAgent(responses=['{"verdict": "needs_changes", "comments": ["handle empty input"]}'])
    s = make_services(agent=stub)
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "needs_changes"
    assert res.update["review_comments"] == ["handle empty input"]
    assert res.goto == "coding_agent"


async def test_reviewer_unparseable_defaults_to_needs_changes():
    from app.graph.nodes.reviewer import reviewer_node
    stub = StubAgent(responses=["looks fine to me"])
    s = make_services(agent=stub)
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "needs_changes"
    assert res.update["review_comments"][0] == "reviewer returned unparseable output"
    assert res.goto == "coding_agent"
