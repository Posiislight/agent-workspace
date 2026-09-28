import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.config import Settings
from app.github.webhooks import classify_followup
from app.graph.build import build_graph
from app.graph.state import initial_state
from app.services.run_manager import AlreadyRunning, FollowupNotAllowed, RunManager
from tests.fakes import FakeRedis, StubGitHub, StubLLM, StubSandbox, make_services

URLS = "URL: https://docs.example.com/api"
OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 1"}],
                  "done": True})
OPS2 = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 2"}],
                   "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})


def _rm(llm_script, settings=None):
    sb = StubSandbox()
    gh = StubGitHub()
    services = make_services(llm=StubLLM(llm_script), sandbox=sb, github=gh,
                             redis=FakeRedis(), settings=settings)
    rm = RunManager(services, build_graph(services, InMemorySaver()))
    return rm, services, sb, gh


def _pushes(sb):
    return [c for c in sb.run_calls if "refs/heads/aw/" in c]


async def test_followup_while_awaiting_approval_recodes_and_repauses():
    rm, services, sb, gh = _rm(["PLAN", URLS, "NOTES", OPS, APPROVED, OPS2, APPROVED])
    st = dict(initial_state("f1", "add feature", "org/repo", "main", "pytest -q", {}))
    await rm.drive("f1", st)
    assert (await rm.get_state("f1"))["status"] == "awaiting_approval"
    assert len(_pushes(sb)) == 1

    task = await rm.followup("f1", "make it async")
    await task
    state = await rm.get_state("f1")
    assert state["status"] == "awaiting_approval"
    assert [f["instruction"] for f in state["followups"]] == ["make it async"]
    assert state["followup_request"] is None
    coding_call = services.llm.calls[5]
    assert "FOLLOW-UP REQUEST (from chat)" in coding_call["messages"][1]["content"]
    assert len(_pushes(sb)) == 2                  # the follow-up revision was pushed
    assert gh.created_prs and len(gh.created_prs) == 1   # same PR, refreshed body
    assert any("## Follow-ups" in f["body"] for _n, f in gh.updated)
    assert any(e.type == "followup" for e in services.publisher.events)


async def test_followup_after_done_starts_at_coding_not_planner():
    rm, services, _sb, _gh = _rm(["PLAN", URLS, "NOTES", OPS, APPROVED, OPS2, APPROVED])
    st = dict(initial_state("f2", "add feature", "org/repo", "main", "pytest -q", {}))
    await rm.drive("f2", st)
    await (await rm.resume("f2", "approved"))
    assert (await rm.get_state("f2"))["status"] == "done"
    n_calls = len(services.llm.calls)

    await (await rm.followup("f2", "add a docstring", source="chat"))
    state = await rm.get_state("f2")
    assert state["status"] == "awaiting_approval"      # back at the gate, same PR
    new_calls = services.llm.calls[n_calls:]
    assert len(new_calls) == 2                          # coding + reviewer only
    assert "add a docstring" in new_calls[0]["messages"][1]["content"]
    assert state["retry_counts"] == {"testing": 0, "coding": 0}


async def test_followup_rejected_when_merged_or_running():
    rm, *_ = _rm(["PLAN", URLS, "NOTES", OPS, APPROVED],
                 settings=Settings(merge_pr_when_ready=True))
    st = dict(initial_state("f3", "x", "org/repo", "main", "pytest -q", {}))
    await rm.drive("f3", st)
    await (await rm.resume("f3", "approved"))
    with pytest.raises(FollowupNotAllowed):
        await rm.followup("f3", "more")

    rm2, *_ = _rm(["PLAN", URLS, "NOTES", OPS, APPROVED])
    running = await rm2.start("f4", dict(initial_state("f4", "x", "org/repo", "main",
                                                        "pytest -q", {})))
    with pytest.raises(AlreadyRunning):
        await rm2.followup("f4", "more")
    await running


def test_classify_followup_comment_and_review_comment():
    base = {"action": "created", "comment": {"body": "/aw make it async",
                                             "user": {"type": "User"}}}
    c = classify_followup("issue_comment", {**base, "issue": {"number": 7, "pull_request": {}}})
    assert c == {"kind": "comment", "pr_number": 7, "instruction": "make it async",
                 "source": "pr_comment"}
    rc = classify_followup("pull_request_review_comment", {
        "action": "created", "pull_request": {"number": 7},
        "comment": {"body": "/aw rename this", "path": "calc.py", "line": 3,
                    "user": {"type": "User"}}})
    assert rc["instruction"].startswith("rename this")
    assert "calc.py:3" in rc["instruction"]
    assert classify_followup("issue_comment", {**base, "issue": {"number": 7}}) is None
    assert classify_followup("issue_comment", {
        "action": "created", "issue": {"number": 7, "pull_request": {}},
        "comment": {"body": "approve", "user": {"type": "User"}}}) is None
    bot = {"action": "created", "issue": {"number": 7, "pull_request": {}},
           "comment": {"body": "/aw x", "user": {"type": "Bot"}}}
    assert classify_followup("issue_comment", bot) is None


def test_classify_followup_failed_check_run_on_task_branch():
    payload = {"action": "completed", "check_run": {
        "id": 99, "name": "tests", "conclusion": "failure", "head_sha": "abcdef123",
        "app": {"slug": "github-actions"}, "check_suite": {"head_branch": "aw/t123"}}}
    c = classify_followup("check_run", payload)
    assert c["kind"] == "ci" and c["task_id"] == "t123" and c["job_id"] == 99
    ok = json.loads(json.dumps(payload))
    ok["check_run"]["conclusion"] = "success"
    assert classify_followup("check_run", ok) is None
    other = json.loads(json.dumps(payload))
    other["check_run"]["check_suite"]["head_branch"] = "feature/x"
    assert classify_followup("check_run", other) is None
