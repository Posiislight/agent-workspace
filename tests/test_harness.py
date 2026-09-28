import pytest

from app.config import Settings
from app.graph.nodes.coding_agent import coding_agent_node
from app.harness import CLI_PREAMBLE, harness_command, validate_harness
from app.sandbox.base import ExecResult
from tests.fakes import StubSandbox, make_services
from tests.test_coding_agent import coding_state


def test_harness_command_substitutes_quoted_brief_path():
    s = Settings(harness_cmd_codex='codex exec --full-auto "$(cat {brief})"')
    assert harness_command(s, "codex", "/data/.runs/b 1.md") == \
        "codex exec --full-auto \"$(cat '/data/.runs/b 1.md')\""


def test_validate_harness_rejects_unknown():
    assert validate_harness("dsh") == "dsh"
    with pytest.raises(ValueError):
        validate_harness("gpt-pilot")


async def test_cli_harness_runs_in_workspace_and_goes_to_tester():
    sb = StubSandbox()
    s = make_services(sandbox=sb)
    res = await coding_agent_node(coding_state(harness="dsh"), services=s)
    assert res.goto == "tester"
    assert res.update["code_diff"].startswith("diff --git")
    brief_path, brief = sb.written[0]
    assert brief_path.startswith("/data/.runs/brief-")
    assert brief.startswith(CLI_PREAMBLE) and "fix add" in brief and "PLAN" in brief
    assert sb.run_calls[0].startswith("dsh --profile headless")
    assert brief_path in sb.run_calls[0]
    assert s.llm.calls == []                    # the harness brings its own model


async def test_cli_harness_failure_goes_to_needs_human_with_log():
    sb = StubSandbox(run_results=[ExecResult(2, "", "codex: auth error")])
    s = make_services(sandbox=sb)
    res = await coding_agent_node(coding_state(harness="codex"), services=s)
    assert res.goto == "needs_human"
    assert "harness exited 2" in res.update["error_log"][-1]
    assert "auth error" in res.update["error_log"][-1]


async def test_cli_harness_empty_diff_goes_to_needs_human():
    sb = StubSandbox(diff_text="")
    s = make_services(sandbox=sb)
    res = await coding_agent_node(coding_state(harness="codex"), services=s)
    assert res.goto == "needs_human"
    assert res.update["error_log"][-1].endswith("empty diff")


async def test_followup_request_is_consumed_and_in_brief():
    sb = StubSandbox()
    s = make_services(sandbox=sb)
    st = coding_state(harness="dsh", followup_request={"source": "chat",
                                                       "instruction": "make it async"})
    res = await coding_agent_node(st, services=s)
    assert res.update["followup_request"] is None
    assert "FOLLOW-UP REQUEST (from chat)" in sb.written[0][1]
    assert "make it async" in sb.written[0][1]
