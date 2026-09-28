import json

from langgraph.checkpoint.memory import InMemorySaver

from app.graph.build import build_graph
from app.graph.nodes.best_of_n import best_of_n_node, candidate_slots
from app.graph.nodes.visual_proof import visual_proof_node
from app.graph.state import initial_state
from app.sandbox.base import ExecResult
from app.sandbox.workspace import TaskWorkspace
from app.services.leases import LeasePool
from app.services.run_manager import RunManager
from tests.fakes import (
    FakeAgent,
    FakeRedis,
    StubComputers,
    StubGitHub,
    StubLLM,
    StubSandbox,
    make_services,
)
from tests.test_coding_agent import coding_state


class MiniRegistry:
    """WorkspaceRegistry stand-in: real TaskWorkspace + LeasePool over FakeAgents."""

    def __init__(self, pool, results=None, delay=0.02):
        self.pool = pool
        self.counter = {"now": 0, "max": 0}
        self.agents: dict[str, FakeAgent] = {}
        self.ws: dict[tuple, TaskWorkspace] = {}
        self.results = results or {}
        self.delay = delay

    def for_candidate(self, state, harness, slot):
        key = f"{state['repo']}:{harness}:{slot}"
        if key not in self.agents:
            self.agents[key] = FakeAgent(
                awake_counter=self.counter, work_delay=self.delay,
                run_result=self.results.get(key, ExecResult(0, "1 passed", "")),
                diff_text=f"diff --git a/{harness}{slot} b/{harness}{slot}\n+x\n")
        wkey = (state["task_id"], key)
        if wkey not in self.ws:
            self.ws[wkey] = TaskWorkspace(self.agents[key], self.pool,
                                          task_id=state["task_id"], base_branch="main",
                                          key=key, pat="p", slot=slot, harness=harness)
        return self.ws[wkey]

    def __call__(self, state):
        return self.for_candidate(state, state.get("harness") or "openrouter",
                                  state.get("workspace_slot") or 0)


def test_candidate_slots_give_duplicates_distinct_vms():
    assert candidate_slots(["codex", "codex", "dsh"]) == [("codex", 0), ("codex", 1),
                                                          ("dsh", 0)]


async def test_best_of_three_runs_two_at_a_time_and_judge_picks_winner():
    pool = LeasePool(max_vms=2)
    reg = MiniRegistry(pool)
    judge = json.dumps({"winner": 2, "scores": {"0": 6, "1": 7, "2": 9},
                        "rationale": "smallest"})
    s = make_services(llm=StubLLM([judge]), sandbox_factory=reg, workspaces=reg, pool=pool)
    st = coding_state(candidates=["codex", "codex", "dsh"], harness="openrouter",
                      test_command="pytest -q")
    res = await best_of_n_node(st, services=s)
    assert reg.counter["max"] == 2                      # plan limit respected
    assert pool.awake == {}                             # everything went back to sleep
    assert res.goto == "reviewer"
    rows = res.update["candidate_results"]
    assert [r["status"] for r in rows] == ["passed"] * 3
    assert [r["winner"] for r in rows] == [False, False, True]
    assert rows[2]["score"] == 9 and rows[2]["insertions"] == 3
    assert res.update["harness"] == "dsh" and res.update["workspace_slot"] == 0
    assert res.update["code_diff"].startswith("diff --git a/dsh0")
    assert res.update["test_results"]["passed"] is True
    ev = [e for e in s.publisher.events if e.type == "candidate_update"]
    assert {e.data["status"] for e in ev} >= {"queued", "coding", "testing", "passed"}


async def test_best_of_n_no_passing_candidate_needs_human():
    pool = LeasePool(max_vms=2)
    fail = ExecResult(1, "FAILED t::x", "")
    reg = MiniRegistry(pool, results={"org/repo:codex:0": fail, "org/repo:dsh:0": fail})
    s = make_services(sandbox_factory=reg, workspaces=reg, pool=pool)
    res = await best_of_n_node(coding_state(candidates=["codex", "dsh"]), services=s)
    assert res.goto == "needs_human"
    assert "no candidate passed" in res.update["error_log"][-1]


async def test_best_of_n_single_passing_candidate_skips_judge():
    pool = LeasePool(max_vms=2)
    reg = MiniRegistry(pool, results={"org/repo:codex:0": ExecResult(1, "FAILED", "")})
    s = make_services(llm=StubLLM([]), sandbox_factory=reg, workspaces=reg, pool=pool)
    res = await best_of_n_node(coding_state(candidates=["codex", "dsh"]), services=s)
    assert res.update["harness"] == "dsh"
    assert s.llm.calls == []


def _proof_state(**kw):
    st = coding_state(preview={"command": "python -m http.server 8000", "port": 8000,
                               "path": "/"})
    st.update(kw)
    return st


async def test_visual_proof_captures_before_after_and_publishes():
    comps = StubComputers()
    gh = StubGitHub()
    redis = FakeRedis()
    pool = LeasePool(max_computers=1)
    s = make_services(computers=comps, github=gh, redis=redis, pool=pool)
    upd = await visual_proof_node(_proof_state(), services=s)
    proof = upd["visual_proof"]
    assert proof["images"] == {"before": "/tasks/t1/proof/before.png",
                               "after": "/tasks/t1/proof/after.png"}
    assert await redis.get("aw:t1:proof:before")
    assert proof["github"]["after"].endswith("/aw-artifacts/proof/t1/after.png?raw=true")
    assert ("aw-artifacts", "proof/t1/before.png") in gh.files
    assert any("origin/main" in c for c in comps.shells)       # before = base
    assert any("origin/aw/t1" in c for c in comps.shells)      # after = task branch
    assert comps.slept == 1 and pool.computer_owners == set()  # lease handed back
    assert any("refs/heads/aw/t1" in c for c in s.sandbox_factory({}).run_calls)


async def test_visual_proof_skips_without_config_and_survives_errors():
    s = make_services()
    upd = await visual_proof_node(coding_state(), services=s)
    assert upd == {"visual_proof": None}

    class Broken(StubComputers):
        async def shell(self, computer_id, command, timeout_s=30):
            return ExecResult(1, "", "git: could not read ghp_secret")
    pool = LeasePool()
    from app.config import Settings
    s = make_services(computers=Broken(), pool=pool, redis=FakeRedis(),
                      settings=Settings(github_pat="ghp_secret"))
    upd = await visual_proof_node(_proof_state(), services=s)
    assert "preview prepare failed" in upd["visual_proof"]["error"]
    assert "ghp_secret" not in upd["visual_proof"]["error"]
    assert pool.computer_owners == set()


async def test_budget_cap_pauses_then_raise_resumes():
    OPS = json.dumps({"ops": [{"op": "write_file", "path": "a.py", "content": "x"}],
                      "done": True})
    APPROVED = json.dumps({"verdict": "approved", "comments": []})
    services = make_services(llm=StubLLM(["PLAN", "URL: https://x.dev", "NOTES", OPS,
                                          APPROVED]),
                             sandbox=StubSandbox(), github=StubGitHub(), redis=FakeRedis())
    rm = RunManager(services, build_graph(services, InMemorySaver()))
    # StubPriceTable charges $0.001 per call: 3 calls before coding -> over a $0.002 cap.
    st = dict(initial_state("b1", "x", "org/repo", "main", "pytest -q", {},
                            budget_usd=0.002))
    await rm.drive("b1", st)
    state = await rm.get_state("b1")
    assert state["status"] == "awaiting_budget"
    assert any(e.type == "budget_exceeded" for e in services.publisher.events)
    await (await rm.raise_budget("b1", 1.0))
    state = await rm.get_state("b1")
    assert state["status"] == "awaiting_approval"
    assert state["budget_usd"] == 1.0
    assert state["llm_cost"] > 0.002


async def test_budget_reject_stops_task():
    services = make_services(llm=StubLLM(["PLAN", "URL: https://x.dev", "NOTES"]),
                             sandbox=StubSandbox(), github=StubGitHub(), redis=FakeRedis())
    rm = RunManager(services, build_graph(services, InMemorySaver()))
    st = dict(initial_state("b2", "x", "org/repo", "main", "pytest -q", {},
                            budget_usd=0.001))
    await rm.drive("b2", st)
    assert (await rm.get_state("b2"))["status"] == "awaiting_budget"
    await (await rm.resume("b2", "rejected"))
    state = await rm.get_state("b2")
    assert state["status"] == "needs_human"
    assert any("stopped at budget cap" in e for e in state["error_log"])
