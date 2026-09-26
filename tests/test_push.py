import pytest

from app.github.push import PushError, branch_for, push_branch
from app.sandbox.base import ExecResult
from tests.fakes import StubSandbox


def test_branch_for_uses_aw_prefix():
    assert branch_for("abc123") == "aw/abc123"


async def test_push_branch_commits_and_pushes_with_pat():
    sb = StubSandbox()
    await push_branch(sb, "org/repo", "aw/t1", "PATSECRET")
    assert len(sb.run_calls) == 1
    cmd = sb.run_calls[0]
    assert "git add -A" in cmd
    assert "git commit" in cmd
    assert "https://x-access-token:PATSECRET@github.com/org/repo.git" in cmd
    assert "HEAD:refs/heads/aw/t1" in cmd
    assert "git remote set-url origin https://github.com/org/repo.git" in cmd


async def test_push_branch_failure_raises_and_scrubs_pat():
    class FailSandbox(StubSandbox):
        async def run_long(self, command, timeout=None):
            self.run_calls.append(command)
            return ExecResult(128, "", "error: failed to push PATSECRET")

    sb = FailSandbox()
    with pytest.raises(PushError) as exc:
        await push_branch(sb, "org/repo", "aw/t1", "PATSECRET")
    assert "PATSECRET" not in str(exc.value)
    assert "***" in str(exc.value)