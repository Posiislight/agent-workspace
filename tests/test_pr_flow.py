from app.github.pr_flow import ensure_draft_pr, render_pr_body
from tests.fakes import StubGitHub, StubSandbox, make_services


def _state():
    return {
        "task_id": "t1", "repo": "org/repo", "base_branch": "main",
        "task_description": "Fix the thing",
        "plan": "1. do it", "code_diff": "diff --git a/x b/x",
        "test_results": {"passed": True},
        "review_comments": ["looks ok"], "cost_so_far": 0.42,
        "retry_counts": {"testing": 1, "coding": 0},
    }


def test_render_pr_body_contains_all_sections():
    body = render_pr_body(_state())
    assert "Fix the thing" in body
    assert "1. do it" in body
    assert "diff --git" in body
    assert "looks ok" in body
    assert "$0.42" in body
    assert "testing: 1" in body


async def test_ensure_draft_pr_creates_and_pushes():
    gh = StubGitHub()
    sb = StubSandbox()
    services = make_services(github=gh, sandbox=sb)
    pr = await ensure_draft_pr(services, _state())
    assert pr["number"] == 11
    assert gh.created_prs[0]["head"] == "aw/t1"
    assert gh.created_prs[0]["base"] == "main"
    assert gh.created_prs[0]["draft"] is True
    assert "Fix the thing" in gh.created_prs[0]["body"]
    assert any("refs/heads/aw/t1" in c for c in sb.run_calls)


async def test_ensure_draft_pr_is_idempotent_via_existing_pr():
    existing = {"number": 5, "html_url": "https://github.com/org/repo/pull/5",
                "draft": True}
    gh = StubGitHub(existing_prs={"org:aw/t1": existing})
    sb = StubSandbox()
    services = make_services(github=gh, sandbox=sb)
    pr = await ensure_draft_pr(services, _state())
    assert pr == existing
    assert gh.created_prs == []          # nothing new created
    assert sb.run_calls == []            # no second push