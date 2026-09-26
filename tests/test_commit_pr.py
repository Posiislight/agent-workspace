from app.config import Settings
from app.graph.nodes.commit_pr import commit_pr_node
from tests.fakes import StubGitHub, StubSandbox, make_services


def _state():
    return {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
            "pr_url": "https://github.com/org/repo/pull/11", "pr_number": 11,
            "cost_so_far": 1.25, "retry_counts": {"testing": 2, "coding": 1},
            "paused_at": "2026-09-26T01:00:00+00:00",
            "resumed_at": "2026-09-26T09:00:00+00:00",
            "task_description": "d"}


async def test_commit_pr_pushes_marks_ready_and_comments():
    gh = StubGitHub()
    sb = StubSandbox()
    services = make_services(github=gh, sandbox=sb)
    result = await commit_pr_node(_state(), services=services)
    assert result == {"status": "done",
                      "pr_url": "https://github.com/org/repo/pull/11"}
    assert any("refs/heads/aw/t1" in c for c in sb.run_calls)
    assert gh.ready == [11]
    assert gh.merged == []
    assert len(gh.comments) == 1
    body = gh.comments[0][1]
    assert "$1.25" in body
    assert "testing: 2" in body and "coding: 1" in body
    assert "paused" in body.lower() or "resume" in body.lower()


async def test_commit_pr_merges_when_configured():
    gh = StubGitHub()
    services = make_services(
        github=gh, sandbox=StubSandbox(),
        settings=Settings(github_pat="p", merge_pr_when_ready=True))
    await commit_pr_node(_state(), services=services)
    assert gh.merged == [11]


async def test_commit_pr_skips_comment_when_final_summary_exists():
    gh = StubGitHub(initial_comments=[{"body": "## Final summary\n- Total cost: $0.50"}])
    services = make_services(github=gh, sandbox=StubSandbox())
    result = await commit_pr_node(_state(), services=services)
    assert result["status"] == "done"
    assert gh.comments == []


async def test_commit_pr_creates_pr_when_state_has_none():
    gh = StubGitHub(existing_prs={"org:aw/t1": {
        "number": 5, "html_url": "https://github.com/org/repo/pull/5",
        "draft": True}})
    services = make_services(github=gh, sandbox=StubSandbox())
    result = await commit_pr_node({"task_id": "t1", "repo": "org/repo",
                                   "base_branch": "main", "cost_so_far": 0.0,
                                   "retry_counts": {}, "task_description": "d"},
                                  services=services)
    assert result["pr_url"] == "https://github.com/org/repo/pull/5"
    assert gh.ready == [5]
