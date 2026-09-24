import pytest


async def test_human_approval_approved(monkeypatch):
    from app.graph.nodes import human_approval
    from tests.fakes import make_services

    async def fake_interrupt(_payload):
        return {"decision": "approved", "feedback": ""}

    monkeypatch.setattr(human_approval, "interrupt", fake_interrupt)
    s = make_services()
    res = await human_approval.human_approval_node(
        {"task_id": "t1", "task_description": "d", "plan": "P", "code_diff": "d",
         "test_results": None, "review_comments": None, "cost_so_far": 0.0}, services=s)
    assert res.update["approval_status"] == "approved"
    assert res.goto == "commit_pr"


async def test_human_approval_rejected_appends_feedback(monkeypatch):
    from app.graph.nodes import human_approval
    from tests.fakes import make_services

    async def fake_interrupt(_payload):
        return {"decision": "rejected", "feedback": "handle empty input"}

    monkeypatch.setattr(human_approval, "interrupt", fake_interrupt)
    s = make_services()
    res = await human_approval.human_approval_node(
        {"task_id": "t1", "task_description": "d", "cost_so_far": 0.0}, services=s)
    assert res.update["approval_status"] == "rejected"
    assert res.update["task_description"].endswith("HUMAN FEEDBACK: handle empty input")
    assert res.goto == "planner"


async def test_needs_human_node():
    from app.graph.nodes.needs_human import needs_human_node
    from tests.fakes import make_services
    st = {"task_id": "t1", "error_log": [], "retry_counts": {"testing": 3, "coding": 0}}
    updates = await needs_human_node(st, services=make_services())
    assert updates["status"] == "needs_human"
    assert "testing=3" in updates["error_log"][-1]


async def test_commit_pr_done():
    from app.graph.nodes.commit_pr import commit_pr_node
    from tests.fakes import make_services
    s = make_services()
    updates = await commit_pr_node({"task_id": "t1", "approval_status": "approved"}, services=s)
    assert updates["status"] == "done"
