

async def test_human_approval_approved(monkeypatch):
    from app.graph.nodes import human_approval
    from tests.fakes import StubGitHub, make_services

    async def fake_interrupt(_payload):
        return {"decision": "approved", "feedback": ""}

    monkeypatch.setattr(human_approval, "interrupt", fake_interrupt)
    s = make_services(github=StubGitHub())
    res = await human_approval.human_approval_node(
        {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
         "task_description": "d", "plan": "P", "code_diff": "d",
         "test_results": None, "review_comments": None, "cost_so_far": 0.0,
         "retry_counts": {}}, services=s)
    assert res.update["approval_status"] == "approved"
    assert res.update["pr_url"].endswith("/pull/11")
    assert res.update["pr_number"] == 11
    assert res.goto == "commit_pr"


async def test_human_approval_rejected_appends_feedback(monkeypatch):
    from app.graph.nodes import human_approval
    from tests.fakes import StubGitHub, make_services

    async def fake_interrupt(_payload):
        return {"decision": "rejected", "feedback": "handle empty input"}

    monkeypatch.setattr(human_approval, "interrupt", fake_interrupt)
    s = make_services(github=StubGitHub())
    res = await human_approval.human_approval_node(
        {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
         "task_description": "d", "cost_so_far": 0.0, "retry_counts": {}}, services=s)
    assert res.update["approval_status"] == "rejected"
    assert res.update["pr_url"].endswith("/pull/11")
    assert res.update["pr_number"] == 11
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


async def test_human_approval_opens_draft_pr_before_interrupt():
    import langgraph.graph as lg
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from app.graph.nodes.human_approval import human_approval_node
    from tests.fakes import StubGitHub, make_services

    gh = StubGitHub()
    services = make_services(github=gh)

    async def node(state):
        return await human_approval_node(state, services=services)

    g = lg.StateGraph(dict)
    g.add_node("human_approval", node)
    g.add_node("sink", lambda s: s)
    g.add_edge(lg.START, "human_approval")
    graph = g.compile(checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "t-pr-gate"}}
    state = {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
             "task_description": "d", "plan": "p", "code_diff": "x",
             "test_results": None, "review_comments": [], "cost_so_far": 0.0,
             "retry_counts": {}}
    it = None
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            it = chunk["__interrupt__"]
    assert it is not None
    payload = it[0].value
    assert payload["pr"]["number"] == 11
    assert payload["pr"]["html_url"].endswith("/pull/11")
    assert gh.created_prs and gh.created_prs[0]["draft"] is True

    resumed = False
    async for chunk in graph.astream(
            Command(resume={"decision": "approved", "feedback": ""}), config,
            stream_mode="updates"):
        resumed = True
    assert resumed
    # idempotent: resume re-run must NOT create a second PR
    assert len(gh.created_prs) == 1
