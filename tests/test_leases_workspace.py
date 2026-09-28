import asyncio

from app.config import Settings
from app.sandbox.workspace import (
    TaskWorkspace,
    WorkspaceRegistry,
    activation_script,
    agent_key,
    parse_shortstat,
    template_for,
)
from app.services.leases import LeasePool
from tests.fakes import FakeAgent


async def test_vm_pool_caps_awake_agents_and_is_reentrant():
    pool = LeasePool(max_vms=2)
    assert await pool.acquire_vm("a", "t1") is False
    assert await pool.acquire_vm("a", "t1") is False   # re-entrant
    await pool.acquire_vm("b", "t2")
    waiter = asyncio.create_task(pool.acquire_vm("c", "t3"))
    await asyncio.sleep(0.01)
    assert not waiter.done()                           # third agent queues
    await pool.release_vm("a", "t1")
    assert await asyncio.wait_for(waiter, 1) is True   # it had to wait
    assert set(pool.awake) == {"b", "c"}


async def test_vm_pool_owner_exclusivity_is_the_repo_mutex():
    pool = LeasePool(max_vms=2)
    await pool.acquire_vm("repo:t:0", "task-A")
    other = asyncio.create_task(pool.acquire_vm("repo:t:0", "task-B"))
    await asyncio.sleep(0.01)
    assert not other.done()                            # same agent, other task: waits
    await pool.release_vm("repo:t:0", "task-A")
    await asyncio.wait_for(other, 1)
    assert pool.awake == {"repo:t:0": "task-B"}


async def test_vm_pool_release_by_non_owner_is_ignored():
    pool = LeasePool(max_vms=1)
    await pool.acquire_vm("a", "t1")
    await pool.release_vm("a", "t2")
    assert pool.holds_vm("a", "t1")


async def test_computer_pool_single_slot_and_on_wait():
    pool = LeasePool(max_computers=1)
    await pool.acquire_computer("t1")
    called = []

    async def on_wait():
        called.append(1)
    w = asyncio.create_task(pool.acquire_computer("t2", on_wait=on_wait))
    await asyncio.sleep(0.01)
    assert called == [1] and not w.done()
    await pool.release_computer("t1")
    await asyncio.wait_for(w, 1)
    assert pool.computer_owners == {"t2"}


def _ws(agent, pool, task_id="t1", key="org/repo:tmpl:0", events=None):
    async def emit(tid, type, data):
        if events is not None:
            events.append((tid, type, data))
    return TaskWorkspace(agent, pool, task_id=task_id, base_branch="main", key=key,
                         pat="ghp_secret", emit=emit)


async def test_workspace_cold_then_warm_activation_and_metrics():
    pool = LeasePool(max_vms=2)
    agent = FakeAgent()
    events = []
    ws = _ws(agent, pool, events=events)
    await ws.ensure()
    first = ws.pop_metrics()
    assert first and first[0]["cold"] is True and first[0]["deps"] == "installed"
    assert any("git checkout" in c for c in agent.execs)
    n_exec = len(agent.execs)
    await ws.exec("echo hi")                    # already active: no re-activation
    assert len(agent.execs) == n_exec + 1
    await ws.sleep()
    assert pool.awake == {}                     # lease returned on sleep
    await ws.ensure()                           # same task wakes: warm, no switch
    assert ws.pop_metrics() == []
    assert ("t1", "workspace_ready", first[0]) in events


async def test_workspace_switches_branch_when_another_task_used_the_agent():
    pool = LeasePool(max_vms=2)
    agent = FakeAgent()
    a, b = _ws(agent, pool, "task-a"), _ws(agent, pool, "task-b")
    await a.ensure()
    await a.sleep()
    await b.ensure()
    m = b.pop_metrics()
    assert m and m[0]["cold"] is False and m[0]["deps"] == "cached"
    assert agent.active_task == "task-b"
    assert "aw/task-b" in agent.execs[-1] or any("aw/task-b" in c for c in agent.execs)


async def test_workspace_exec_auto_ensures_after_sleep():
    pool = LeasePool(max_vms=1)
    agent = FakeAgent()
    ws = _ws(agent, pool)
    await ws.ensure()
    await ws.sleep()
    await ws.run_long("git push")               # e.g. commit_pr after a paused approval
    assert pool.holds_vm(ws.key, "t1")


async def test_workspace_vm_seconds_accrue_while_owned():
    pool = LeasePool()
    ws = _ws(FakeAgent(), pool)
    await ws.ensure()
    await asyncio.sleep(0.02)
    await ws.sleep()
    secs = ws.take_vm_seconds()
    assert secs >= 0.02
    assert ws.take_vm_seconds() == 0.0


async def test_diff_is_against_merge_base():
    ws = _ws(FakeAgent(), LeasePool())
    await ws.diff()
    assert "git merge-base HEAD main" in ws.agent.execs[-1]


def test_activation_script_parks_wip_and_restores_remote_branch():
    s = activation_script("org/repo", "abc", "main", "ghp_x")
    assert "git commit -qm 'aw: wip (parked)'" in s
    assert "refs/remotes/origin/aw/abc" in s
    assert "git checkout -q -B aw/abc origin/main" in s


def test_parse_shortstat():
    assert parse_shortstat(" 2 files changed, 10 insertions(+), 3 deletions(-)") == {
        "files": 2, "insertions": 10, "deletions": 3}
    assert parse_shortstat("") == {"files": 0, "insertions": 0, "deletions": 0}


def test_registry_keys_agents_by_repo_template_slot():
    s = Settings(maritime_template_id="base", maritime_template_codex="codex-tpl",
                 harness_env={"OPENAI_API_KEY": "k"})
    reg = WorkspaceRegistry(s, client=None, pool=LeasePool())
    st = {"task_id": "t1", "repo": "org/repo", "base_branch": "main"}
    w_default = reg(st)
    w_codex1 = reg.for_candidate(st, "codex", 1)
    assert w_default.key == agent_key("org/repo", "base", 0)
    assert w_codex1.key == "org/repo:codex-tpl:1"
    assert reg(st) is w_default                      # cached per task
    other = reg({**st, "task_id": "t2"})
    assert other.agent is w_default.agent            # same repo agent, different task
    assert w_codex1.agent.external_id == "aw-repo:org/repo:codex-tpl:1"
    assert w_codex1.agent.env == {"OPENAI_API_KEY": "k"}
    assert w_default.agent.env == {}
    assert template_for(s, "dsh") == s.maritime_template_dsh
