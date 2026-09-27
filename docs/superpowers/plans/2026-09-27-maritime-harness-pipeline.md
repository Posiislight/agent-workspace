# Maritime-Harness Pipeline (v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace direct OpenRouter LLM calls with Maritime agent harnesses — one Maritime agent per task (template chosen by the user: `codex` or `dsh`), every LLM stage driven by `POST /api/agents/{id}/chat` against Maritime's LLM proxy.

**Architecture:** A new `MaritimeAgentClient` wraps agent create/wait/chat/sleep. At task start the RunManager provisions one Maritime agent, waits for `active`, provisions the repo clone inside that same agent VM (the harness's workspace IS the repo workspace), and every graph node chats with it using per-stage `conversation_id`s. Test execution stays orchestrator-side (`run_long` pytest) for deterministic routing. Cost = VM compute only.

**Tech Stack:** FastAPI, LangGraph, httpx, asyncpg/redis, React 18 + Vite + react-router.

**Spec:** `docs/superpowers/specs/2026-09-27-maritime-harness-pipeline-design.md`

## Global Constraints

- Templates allowlist is exactly `["codex", "dsh"]` (verified ready-to-run on Maritime).
- Chat timeout 600s; chat responses arrive as `{"response": "<text>"}`.
- Agents must be polled to `status == "active"` before chat; poll every 5s, give up after 180s (`AgentTimeout`).
- Per-stage conversation ids: `aw-{task_id}-{stage}`; on re-entry (retry after test failure / review changes / restart) append `-r{retry_count}` so stages get a clean thread.
- Repo workspace path in VM: `/data/workspace` (existing constants in `app/sandbox/maritime.py`).
- `POST /tasks` requires `template_id` (422 if missing or not in allowlist); `model_overrides` is gone.
- Test command defaults from `settings.test_command` ("pytest -q"); tester verdicts come from orchestrator-run tests, never from chat.
- Run `pytest` from repo root; Windows venv: `.venv\Scripts\python.exe -m pytest`.
- Existing test helpers live in `tests/fakes.py` (`make_services`, `StubLLM`, `StubSandbox`).

---

### Task 1: MaritimeAgentClient

**Files:**
- Create: `app/sandbox/maritime_agent.py`
- Test: `tests/test_maritime_agent_client.py`

**Interfaces:**
- Consumes: `app.llm.backoff.with_retry(fn, attempts, base_delay)`; httpx `AsyncClient` with `base_url` already set to Maritime.
- Produces: `class AgentTimeout(RuntimeError)`; `class MaritimeAgentClient` with `async create(name, template_id) -> str`, `async wait_active(agent_id, timeout_s=180, poll_s=5) -> str`, `async chat(agent_id, message, conversation_id, timeout_s=600) -> str`, `async llm_status(agent_id) -> dict`, `async sleep(agent_id) -> None`, `async total_compute_seconds(agent_id) -> float`, `async get(agent_id) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
import json
import httpx
import pytest
from app.sandbox.maritime_agent import AgentTimeout, MaritimeAgentClient


def make_client(handler) -> MaritimeAgentClient:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.maritime.sh", transport=transport)
    return MaritimeAgentClient("k", client=client)


async def test_create_posts_template_and_returns_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents"
        body = json.loads(request.content)
        assert body["templateId"] == "codex"
        return httpx.Response(201, json={"id": "a-1"})
    c = make_client(handler)
    assert await c.create("aw-task-x", "codex") == "a-1"


async def test_wait_active_polls_until_active():
    states = ["deploying", "deploying", "active"]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/a-1"
        return httpx.Response(200, json={"status": states.pop(0)})
    c = make_client(handler)
    assert await c.wait_active("a-1", timeout_s=30, poll_s=0) == "a-1"


async def test_wait_active_times_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "deploying"})
    c = make_client(handler)
    with pytest.raises(AgentTimeout):
        await c.wait_active("a-1", timeout_s=0.2, poll_s=0.05)


async def test_chat_returns_response_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/a-1/chat"
        body = json.loads(request.content)
        assert body["message"] == "hi"
        assert body["conversation_id"] == "conv-1"
        return httpx.Response(200, json={"response": "PONG"})
    c = make_client(handler)
    assert await c.chat("a-1", "hi", "conv-1") == "PONG"


async def test_chat_retries_transient_errors():
    calls = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"response": "ok"})
    c = make_client(handler)
    assert await c.chat("a-1", "m", "c") == "ok"
    assert calls["n"] == 3


async def test_sleep_and_compute_seconds():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sleep"):
            return httpx.Response(200, json={})
        assert request.url.path == "/api/agents/a-1"
        return httpx.Response(200, json={"status": "sleeping", "totalComputeSeconds": 125})
    c = make_client(handler)
    await c.sleep("a-1")
    assert await c.total_compute_seconds("a-1") == 125.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maritime_agent_client.py -v`
Expected: FAIL — `ModuleNotFoundError: app.sandbox.maritime_agent`

- [ ] **Step 3: Implement the client**

```python
import asyncio

import httpx

from app.llm.backoff import with_retry


class AgentTimeout(RuntimeError):
    pass


class MaritimeAgentClient:
    """Maritime REST wrapper: agent lifecycle + harness chat (LLM proxy)."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url="https://api.maritime.sh", timeout=600)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        return await self._client.request(method, path, headers=self._headers, **kw)

    async def create(self, name: str, template_id: str) -> str:
        r = await self._req("POST", "/api/agents",
                            json={"name": name, "templateId": template_id})
        r.raise_for_status()
        return r.json()["id"]

    async def get(self, agent_id: str) -> dict:
        r = await self._req("GET", f"/api/agents/{agent_id}")
        r.raise_for_status()
        return r.json()

    async def wait_active(self, agent_id: str, timeout_s: float = 180,
                          poll_s: float = 5) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            d = await self.get(agent_id)
            if d.get("status") == "active":
                return agent_id
            if asyncio.get_event_loop().time() >= deadline:
                raise AgentTimeout(
                    f"agent {agent_id} not active after {timeout_s}s "
                    f"(status={d.get('status')})")
            await asyncio.sleep(poll_s)

    async def chat(self, agent_id: str, message: str, conversation_id: str,
                   timeout_s: int = 600) -> str:
        async def _call() -> str:
            r = await self._req("POST", f"/api/agents/{agent_id}/chat",
                                json={"message": message,
                                      "conversation_id": conversation_id},
                                timeout=timeout_s)
            r.raise_for_status()
            return r.json()["response"]
        return await with_retry(_call)

    async def llm_status(self, agent_id: str) -> dict:
        r = await self._req("GET", f"/api/agents/{agent_id}/llm-status")
        r.raise_for_status()
        return r.json()

    async def sleep(self, agent_id: str) -> None:
        await self._req("POST", f"/api/agents/{agent_id}/sleep")

    async def total_compute_seconds(self, agent_id: str) -> float:
        return float((await self.get(agent_id)).get("totalComputeSeconds") or 0.0)
```

Note: `with_retry` retries on 503 (`_TRANSIENT_STATUSES` includes it). Pass `sleep=...` no-op in unit tests only if retry backoff makes tests slow — the base delay is 1.0s; with 3 attempts that's 1s + 4s worst case. For the retry test, monkeypatch `app.sandbox.maritime_agent.asyncio.sleep` or accept the delay. Simplest: in `test_chat_retries_transient_errors`, patch `with_retry`'s sleep by passing through a module-level `sleep_fn` — instead, just set `c._retry_base = 0.0`:

Add to the client: `self._retry_base = 1.0` and in `chat`: `return await with_retry(_call, base_delay=self._retry_base)`. Tests set `c._retry_base = 0`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maritime_agent_client.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/sandbox/maritime_agent.py tests/test_maritime_agent_client.py
git commit -m "feat: MaritimeAgentClient for agent lifecycle and harness chat"
```

---

### Task 2: Services wiring, settings, and template allowlist

**Files:**
- Modify: `app/graph/nodes/helpers.py` (Services dataclass: add `agent` field)
- Modify: `app/config.py` (add `templates_allowlist`, drop model settings usage later)
- Modify: `tests/fakes.py` (StubAgent + make_services wiring)
- Test: `tests/test_node_helpers.py` (add conversation-id helper tests)

**Interfaces:**
- Consumes: `MaritimeAgentClient` from Task 1.
- Produces: `Services.agent: object` (the client); `ALLOWED_TEMPLATES = ("codex", "dsh")` in `app/config.py`; `conversation_id(task_id, stage, retry) -> str` helper in `app/graph/nodes/helpers.py` returning `aw-{task_id}-{stage}` or `aw-{task_id}-{stage}-r{retry}` when retry > 0; `StubAgent` in fakes with `async chat(agent_id, message, conversation_id, timeout_s=None) -> str`, records `self.calls` as `{"agent_id", "message", "conversation_id"}`, scripted FIFO `responses`; `create/wait_active/sleep/total_compute_seconds` stubs recording calls.

- [ ] **Step 1: Write failing test for conversation_id**

Append to `tests/test_node_helpers.py`:

```python
async def test_conversation_id_plain_and_retry():
    from app.graph.nodes.helpers import conversation_id
    assert conversation_id("t1", "planner", 0) == "aw-t1-planner"
    assert conversation_id("t1", "coding", 2) == "aw-t1-coding-r2"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_node_helpers.py::test_conversation_id_plain_and_retry -v`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement**

In `app/config.py` add (after `maritime_template_id`):

```python
    templates_allowlist: tuple[str, ...] = ("codex", "dsh")
```

In `app/graph/nodes/helpers.py` add to `Services`: `agent: object = None`, and:

```python
def conversation_id(task_id: str, stage: str, retry: int = 0) -> str:
    base = f"aw-{task_id}-{stage}"
    return base if not retry else f"{base}-r{retry}"
```

- [ ] **Step 4: Add StubAgent to tests/fakes.py**

```python
class StubAgent:
    """FIFO-scripted MaritimeAgentClient replacement. Records calls."""

    def __init__(self, responses=None, agent_id="agent-1", compute_seconds=120.0):
        self.agent_id = agent_id
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.created: list[str] = []   # template ids
        self.waited: list[str] = []
        self.slept: list[str] = []
        self.compute_seconds = compute_seconds

    async def create(self, name, template_id):
        self.created.append(template_id)
        return self.agent_id

    async def wait_active(self, agent_id, timeout_s=180, poll_s=5):
        self.waited.append(agent_id)
        return agent_id

    async def chat(self, agent_id, message, conversation_id, timeout_s=None):
        self.calls.append({"agent_id": agent_id, "message": message,
                           "conversation_id": conversation_id})
        if not self.responses:
            raise AssertionError("StubAgent exhausted — add scripted responses")
        return self.responses.pop(0)

    async def sleep(self, agent_id):
        self.slept.append(agent_id)

    async def total_compute_seconds(self, agent_id):
        return self.compute_seconds

    async def llm_status(self, agent_id):
        return {"has_key": True, "using_maritime": True}
```

In `make_services`, add parameter `agent=None` and pass `agent=agent or StubAgent()` into `Services(...)`.

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_node_helpers.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/graph/nodes/helpers.py tests/fakes.py tests/test_node_helpers.py
git commit -m "feat: Services.agent wiring, template allowlist, conversation_id helper"
```

---

### Task 3: Task agent lifecycle in RunManager (create once, sleep at end)

**Files:**
- Modify: `app/services/run_manager.py` (agent create/wait in `drive` before first graph step; sleep in `finally` and on pause)
- Test: `tests/test_run_manager_agent.py` (new)

**Interfaces:**
- Consumes: `services.agent` (StubAgent/MaritimeAgentClient), `services.sandbox_factory(state)` — must now bind the sandbox to the shared agent (Task 4 handles binding; here we only orchestrate lifecycle).
- Produces: graph state key `agent_id: str` (set before graph start via initial input); `RunManager` ensures agent exists exactly once per task run: `agent_id = await services.agent.create(f"aw-task-{task_id}", template_id)` then `wait_active`; sleeps agent in `drive`'s `finally` and at interrupt pause. `template_id` read from state `template_id`.

- [ ] **Step 1: Write failing test**

```python
import asyncio
import pytest
from langgraph.graph import END, START, StateGraph
from tests.fakes import FakeRedis, StubAgent, StubSandbox, make_services
from app.graph.state import TaskState


def make_one_node_graph():
    def passthrough(state):
        return {"status": "planning"}
    b = StateGraph(TaskState)
    b.add_node("planner", passthrough)
    b.add_edge(START, "planner")
    b.add_edge("planner", END)
    return b.compile()


def make_state(task_id="t-agent"):
    return {"task_id": task_id, "repo": "o/r", "task_description": "d",
            "base_branch": "main", "test_command": "pytest -q", "plan": None,
            "research_notes": None, "code_diff": None, "test_results": None,
            "review_comments": None, "approval_status": "pending",
            "retry_counts": {}, "cost_so_far": 0.0, "status": "planning",
            "error_log": [], "model_overrides": {}, "paused_at": None,
            "resumed_at": None, "pr_url": None, "template_id": "codex"}


async def test_drive_creates_and_sleeps_agent():
    agent = StubAgent()
    s = make_services(agent=agent, redis=FakeRedis(), sandbox=StubSandbox())
    from app.services.run_manager import RunManager
    rm = RunManager(s, make_one_node_graph())
    st = make_state()
    await rm.prepare("t-agent", st)
    await rm.start("t-agent", st)
    await asyncio.wait_for(rm._drives["t-agent"], timeout=5)
    assert agent.created == ["codex"]
    assert agent.waited == ["agent-1"]
    assert agent.slept == ["agent-1"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_run_manager_agent.py -v`
Expected: FAIL — agent.created == [] (lifecycle not implemented)

- [ ] **Step 3: Implement in RunManager.drive**

Before the graph stream (after tracker start), add:

```python
        agent = getattr(self.services, "agent", None)
        agent_id = graph_input.get("agent_id") if isinstance(graph_input, dict) else None
        if agent is not None and agent_id is None:
            template = graph_input.get("template_id", "codex")
            agent_id = await agent.create(f"aw-task-{task_id}", template)
            await agent.wait_active(agent_id)
```

and make the graph input the dict `{**graph_input, "agent_id": agent_id}` when `graph_input` is a dict (leave `Command` resume inputs untouched — on resume, reuse `state["agent_id"]` from `get_state` if graph_input is a Command: `agent_id = (await self.get_state(task_id) or {}).get("agent_id")`).

In the interrupt-pause block (where `sandbox.sleep()` is called today) add, guarded best-effort:

```python
                    if agent is not None and agent_id:
                        try:
                            await agent.sleep(agent_id)
                        except Exception:  # noqa: BLE001, S110
                            pass
```

In the `finally`, same best-effort sleep when the drive is ending (only if not paused — at pause we already slept; double sleep is harmless but skip for cleanliness).

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_run_manager_agent.py tests/test_run_manager_restart.py tests/test_run_manager_failures.py -v`
Expected: PASS (fix regressions by adding `template_id`/`agent_id` to fixture states in existing tests only where the new code path reads them — fakes' make_services default agent is StubAgent so old tests will exercise create/sleep; assert nothing new there)

- [ ] **Step 5: Commit**

```bash
git add app/services/run_manager.py tests/test_run_manager_agent.py
git commit -m "feat: per-task Maritime agent lifecycle in RunManager"
```

---

### Task 4: Bind sandbox to the shared agent

**Files:**
- Modify: `app/sandbox/maritime.py` (`MaritimeSandbox.ensure` uses existing agent when state carries `agent_id`)
- Modify: `app/main.py` `_sandbox_for` (pass state through to bind `agent_id`)
- Modify: `app/graph/nodes/helpers.py` `Services.sandbox_factory` contract comment only
- Test: `tests/test_maritime_sandbox.py` (extend)

**Interfaces:**
- Consumes: state `agent_id` (from Task 3), existing `MaritimeSandbox`.
- Produces: `MaritimeSandbox` whose `ensure()` binds to `state["agent_id"]` when present instead of creating its own agent; provisioning (repo clone + venv) then runs inside the harness agent's VM. `make_sandbox(settings, task_id, repo, base_branch, client=None, agent_id=None)`.

- [ ] **Step 1: Extend sandbox test**

Append to `tests/test_maritime_sandbox.py` (keep existing tests; add):

```python
async def test_ensure_binds_to_provided_agent_id(httpx_mock_transport):
    # maritime.py tests already construct clients via httpx.MockTransport-style
    # fakes; reuse the existing fixture pattern from this module. The assertion:
    # with agent_id preset, ensure() must NOT POST /api/agents, must GET the
    # agent, and must run the provision script via exec.
```

Concretely (using the module's existing fake-transport helper; if the module instead subclasses `MaritimeSandbox` with a stub `_client`, mirror that pattern):

```python
async def test_ensure_reuses_existing_agent(monkeypatch):
    import app.sandbox.maritime as m
    posted = []
    class FakeResp:
        status_code = 200
        def json(self): return {"exitCode": 0, "stdout": "", "stderr": ""}
    class FakeClient:
        async def post(self, url, headers=None, json=None):
            posted.append(url)
            if url.endswith("/exec"):
                return FakeResp()
            raise AssertionError(f"unexpected POST {url}")
        async def get(self, url, headers=None, params=None):
            class R:
                status_code = 200
            return R()
    s = m.MaritimeSandbox.__new__(m.MaritimeSandbox)
    s._s = type("S", (), {"github_pat": "p", "sandbox_exec_timeout_seconds": 10})()
    s._client = FakeClient()
    s.task_id = "t"
    s.repo = "o/r"
    s.base_branch = "main"
    s.agent_id = "preexisting-agent"
    assert await s.ensure() == "preexisting-agent"
    assert all("/exec" in p for p in posted), posted
```

- [ ] **Step 2: Run to verify failure/pass analysis**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maritime_sandbox.py -v`
Expected: this test likely PASSES already (ensure() returns early when agent_id set — verify by reading `ensure()`; the current code GETs and returns if 200). If it passes, the change needed is elsewhere: `app/main.py` `_sandbox_for` must pass `agent_id=state.get("agent_id")` and `make_sandbox` must accept it:

```python
def make_sandbox(settings, task_id: str, repo: str, base_branch: str,
                 client=None, agent_id: str | None = None) -> MaritimeSandbox:
    client = client or httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
    sb = MaritimeSandbox(settings, client, task_id, repo, base_branch)
    if agent_id:
        sb.agent_id = agent_id
    return sb
```

and `_sandbox_for` in `app/main.py`:

```python
        def _sandbox_for(state):
            tid = state["task_id"]
            if tid not in sandbox_cache:
                sandbox_cache[tid] = make_sandbox(
                    settings, tid, state["repo"], state["base_branch"],
                    client=http_maritime, agent_id=state.get("agent_id"))
            return sandbox_cache[tid]
```

- [ ] **Step 3: Run sandbox + main tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maritime_sandbox.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/sandbox/maritime.py app/main.py tests/test_maritime_sandbox.py
git commit -m "feat: sandbox binds to shared task agent VM"
```

---

### Task 5: Rewrite graph nodes to chat with the harness

**Files:**
- Modify: `app/graph/nodes/planner.py`
- Modify: `app/graph/nodes/researcher.py`
- Modify: `app/graph/nodes/coding_agent.py`
- Modify: `app/graph/nodes/reviewer.py`
- Test: `tests/test_planner.py`, `tests/test_researcher.py`, `tests/test_coding_agent.py`, `tests/test_reviewer.py` (update), plus run `tests/test_tester.py` unchanged

**Interfaces:**
- Consumes: `services.agent.chat(agent_id, message, conversation_id)`; `conversation_id(task_id, stage, retry)`; state `agent_id`, `retry_counts`.
- Produces: nodes replace `chat_with_retry(services, state, role, messages)` with `services.agent.chat(state["agent_id"], prompt, conversation_id(...))`. Planner/researcher/reviewer keep their parse logic; coding drops the ops JSON protocol entirely (one chat turn; orchestrator harvests diff). `apply_llm_cost` REMOVED from all nodes — cost is VM-only now (Task 7).

- [ ] **Step 1: Update planner test first**

In `tests/test_planner.py`, replace `StubLLM`-based wiring with `StubAgent(responses=["PLAN TEXT"])` via `make_services(agent=stub)`, and assert:

```python
    stub = StubAgent(responses=["PLAN TEXT"])
    s = make_services(agent=stub, sandbox=StubSandbox())
    updates = await planner_node(make_state(), services=s)
    assert updates["plan"] == "PLAN TEXT"
    call = stub.calls[0]
    assert call["conversation_id"] == "aw-t1-planner"
    assert "Task:" in call["message"]
```

- [ ] **Step 2: Run planner test to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py -v`
Expected: FAIL (planner still calls services.llm)

- [ ] **Step 3: Rewrite planner_node**

```python
from app.graph.nodes.helpers import conversation_id, emit


async def planner_node(state, *, services):
    await emit(services, state, "planner", "node_started", {})
    await emit(services, state, "planner", "tool_call", {"action": "read_file_tree"})
    tree = await _file_tree(state, services)
    prior = ""
    if state.get("plan") and state.get("approval_status") == "rejected":
        prior = (f"\n\nPREVIOUS PLAN (revise it, addressing all human feedback; "
                 f"do not silently drop any point):\n{state['plan']}")
    prompt = (PLANNER_PROMPT +
              f"\n\nRepository: {state['repo']} (base branch {state['base_branch']})\n"
              f"File tree (depth 2):\n{tree}\n\nTask: {state['task_description']}{prior}")
    cid = conversation_id(state["task_id"], "planner",
                          state["retry_counts"].get("coding", 0))
    plan = await services.agent.chat(state["agent_id"], prompt, cid)
    updates = {"plan": plan, "status": "researching", "error_log": list(state["error_log"])}
    await emit(services, state, "planner", "node_completed", {"plan_chars": len(plan)})
    return updates
```

(Keep `_file_tree` unchanged.)

- [ ] **Step 4: Researcher — same pattern**

Replace both `chat_with_retry(services, state, "researcher", ...)` calls with `services.agent.chat(state["agent_id"], prompt_text, conversation_id(state["task_id"], "researcher", 0))`. Prompt = `RESEARCHER_PROMPT + "\n\n" + user-content` (same content as the old user message). Delete `apply_llm_cost` lines and `chat_with_retry` import. Keep browser flow, `_urls`, `_fetch_excerpt`.

- [ ] **Step 5: Coding agent — full rewrite**

```python
from langgraph.types import Command

from app.graph.nodes.helpers import conversation_id, emit

CODING_PROMPT = """You are the Coding Agent working inside a VM that already has the
target repository cloned at /data/workspace (base branch {base}). Implement the task
below directly in that checkout using your own tools: edit files and run commands
yourself. Do NOT git commit or push; leave the finished change as uncommitted
modifications. When you believe the work is complete, reply DONE on its own line,
otherwise describe what blocked you.

{context}
TASK:
{task}

PLAN:
{plan}

RESEARCH NOTES:
{notes}"""

MAX_ROUNDS = 8


async def coding_agent_node(state, *, services):
    await emit(services, state, "coding_agent", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()

    updates = {"status": "coding", "error_log": list(state["error_log"]),
               "retry_counts": dict(state["retry_counts"])}
    context = ""
    if state.get("review_comments"):
        context = "REVIEW COMMENTS TO ADDRESS:\n" + "\n".join(state["review_comments"])
        updates["review_comments"] = None
        updates["retry_counts"] = {**state["retry_counts"],
                                   "coding": state["retry_counts"].get("coding", 0) + 1}
    elif state.get("test_results") and not state["test_results"]["passed"]:
        context = ("TEST FAILURE DETAILS:\n"
                   + (state["test_results"].get("failing_output") or ""))
        updates["retry_counts"] = {**state["retry_counts"],
                                   "testing": state["retry_counts"].get("testing", 0) + 1}

    prompt = CODING_PROMPT.format(
        base=state["base_branch"], context=context,
        task=state["task_description"], plan=state.get("plan") or "(none)",
        notes=state.get("research_notes") or "(none)")
    retry = updates["retry_counts"].get("coding", 0)
    cid = conversation_id(state["task_id"], "coding", retry)
    await services.agent.chat(state["agent_id"], prompt, cid)

    diff = await sb.diff()
    if not diff.strip():
        updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
        return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
    updates["code_diff"] = diff
    updates["status"] = "testing"
    await emit(services, state, "coding_agent", "node_completed", {"diff_chars": len(diff)})
    return Command(update=updates, goto="tester")
```

- [ ] **Step 6: Reviewer — same pattern**

Replace the messages block with a single prompt (REVIEWER_PROMPT + task/plan/test-results/diff), call `services.agent.chat(state["agent_id"], prompt, conversation_id(state["task_id"], "reviewer", state["retry_counts"].get("coding", 0)))`, keep `_parse_verdict`, delete `apply_llm_cost` usage.

- [ ] **Step 7: Update all four node tests**

Each of `tests/test_planner.py`, `tests/test_researcher.py`, `tests/test_coding_agent.py`, `tests/test_reviewer.py`: swap `StubLLM` → `StubAgent(responses=[...])`, add `"agent_id": "agent-1", "template_id": "codex"` to fixture states, delete `model_overrides` from fixtures where asserted, and delete `cost_so_far` assertions tied to `apply_llm_cost` (StubPriceTable math is gone from nodes). For `test_coding_agent.py`: remove tests of the ops-JSON round protocol (`_parse_round`, malformed-round retry, write_file/shell op emission) and replace with: empty-diff → needs_human test (StubSandbox with `diff_text=""`), and success test asserting `updates["status"] == "testing"`, `goto` route, and one chat call with conversation id `aw-t1-coding`.

- [ ] **Step 8: Run all node tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_researcher.py tests/test_coding_agent.py tests/test_reviewer.py tests/test_tester.py tests/test_gate_nodes.py -v`
Expected: PASS (tester untouched; gate/edges untouched)

- [ ] **Step 9: Commit**

```bash
git add app/graph/nodes tests/test_planner.py tests/test_researcher.py tests/test_coding_agent.py tests/test_reviewer.py
git commit -m "feat: graph nodes drive Maritime harness via chat"
```

---

### Task 6: API routes — GET /templates, POST /tasks template_id

**Files:**
- Modify: `app/api/routes_meta.py` (add `/templates`)
- Modify: `app/api/routes_tasks.py` (`TaskCreate.template_id: str` required; validate against `settings.templates_allowlist`; pass into `initial_state`; drop `model_overrides`)
- Modify: `app/graph/state.py` `initial_state(...)` signature: `(task_id, description, repo, base_branch, test_command, template_id)` — add `template_id`, remove `model_overrides`
- Test: `tests/test_templates_route.py` (new), `tests/test_api_flow.py` + `tests/test_restart_route.py` (update fixtures)

**Interfaces:**
- Consumes: `settings.templates_allowlist` (Task 2), `initial_state`.
- Produces: `GET /templates` → `{"templates": [{"id": "codex", "name": "Codex", "description": "...", "tags": [...]}]}` (static metadata dict; ids from allowlist). `POST /tasks` 422 on unknown template.

- [ ] **Step 1: Write failing tests**

`tests/test_templates_route.py` (pattern follows `tests/test_api_flow.py`'s app construction — reuse its `client` fixture approach; the meta routes only need `app.state.services.settings`):

```python
from fastapi.testclient import TestClient
from types import SimpleNamespace
from app.main import create_app


def make_client():
    app = create_app(services=SimpleNamespace(
        settings=SimpleNamespace(github_pat="k", templates_allowlist=("codex", "dsh")),
        llm=None))
    return TestClient(app)


def test_templates_lists_allowlist():
    c = make_client()
    r = c.get("/templates")
    assert r.status_code == 200
    ids = [t["id"] for t in r.json()["templates"]]
    assert ids == ["codex", "dsh"]
```

And in `tests/test_api_flow.py` (or wherever POST /tasks route tests live — grep for `create_task` usage): update create bodies to include `"template_id": "codex"` and add:

```python
def test_create_task_rejects_unknown_template(...):
    r = client.post("/tasks", json={..., "template_id": "claude_code"})
    assert r.status_code == 422
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_templates_route.py -v`
Expected: FAIL — 404 on /templates

- [ ] **Step 3: Implement routes**

In `routes_meta.py`:

```python
TEMPLATE_META = {
    "codex": {"name": "Codex", "description": "OpenAI Codex CLI in a persistent workspace.",
              "tags": ["Coding", "OpenAI"]},
    "dsh": {"name": "DeepSeek Harness", "description": "DeepSeek open-source agent harness.",
            "tags": ["Coding", "DeepSeek"]},
}


@router.get("/templates")
async def templates(request: Request):
    allow = getattr(request.app.state.services.settings, "templates_allowlist",
                    ("codex", "dsh"))
    return {"templates": [{"id": t, **TEMPLATE_META.get(t, {"name": t, "description": "", "tags": []})}
                          for t in allow]}
```

In `routes_tasks.py`: `TaskCreate` gains `template_id: str`; validation:

```python
    allow = getattr(request.app.state.services.settings, "templates_allowlist",
                    ("codex", "dsh"))
    if body.template_id not in allow:
        raise HTTPException(422, f"template_id must be one of {list(allow)}")
```

and pass `body.template_id` into `initial_state(...)` in place of `body.model_overrides`.

In `app/graph/state.py` `initial_state`: replace the `model_overrides` param with `template_id: str = "codex"`, and set state fields `template_id: template_id` (remove `model_overrides`). Read the file first and adjust the `TaskState` TypedDict/class accordingly (add `template_id`, remove `model_overrides`).

- [ ] **Step 4: Update dependent tests**

Grep `model_overrides` across `tests/` and `app/` and update: state fixtures get `"template_id": "codex"` (and keep `"agent_id": "agent-1"` from Task 5 work); route test bodies add `template_id`. `tests/test_run_manager_vm_cost.py`, `tests/test_api_flow.py`, `tests/test_graph_scenarios.py`, `tests/test_restart_resume.py` are the likely ones.

- [ ] **Step 5: Run full unit suite**

Run: `.venv\Scripts\python.exe -m pytest tests -x -q --ignore=tests/e2e --ignore=tests/integration -k "not phase1_real"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/api app/graph/state.py tests
git commit -m "feat: template selection API (GET /templates, POST /tasks template_id)"
```

---

### Task 7: Cost = VM compute; remove OpenRouter from the graph path

**Files:**
- Modify: `app/graph/nodes/helpers.py` (delete `model_for`, `chat_with_retry`, `apply_llm_cost`)
- Modify: `app/main.py` lifespan (no `PriceTable.fetch`; `Services` gets `agent=MaritimeAgentClient(settings.maritime_api_key, http_maritime)`, keep `llm` out or leave the OpenRouter client off)
- Modify: `app/services/run_manager.py` `_mirror` (`cost_so_far` = vm_cost from tracker snapshot)
- Test: `tests/test_node_helpers.py` (delete `model_for`/`apply_llm_cost` tests), `tests/test_run_manager_vm_cost.py` (assert `cost_so_far` mirrors vm cost)

**Interfaces:**
- Consumes: `CostTracker.snapshot(task_id)` → `{"llm_cost", "vm_cost", "vm_minutes"}` (exists).
- Produces: `_mirror` writes `pg_values["cost_so_far"] = snap["vm_cost"] or 0.0` and mirrors `llm_cost: 0.0`. `app.state.services.agent` is the real `MaritimeAgentClient` sharing `http_maritime`.

- [ ] **Step 1: Update the mirror test**

In `tests/test_run_manager_vm_cost.py`, add assertion after an existing mirror pass: `state["cost_so_far"] == state["vm_cost"]` (tracker snapshot vm_cost). Write the concrete test following the file's existing fixture style.

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_run_manager_vm_cost.py -v`
Expected: FAIL (cost_so_far is llm-based today)

- [ ] **Step 3: Implement**

`run_manager._mirror`:

```python
            pg_values = {**values, "cost_so_far": snap["vm_cost"] or 0.0,
                         "llm_cost": 0.0,
                         "vm_cost": snap["vm_cost"],
                         "vm_minutes": snap["vm_minutes"]}
```

(redis mirror line: replace `"llm_cost": snap["llm_cost"]` with `"llm_cost": 0.0`.)

`app/main.py` lifespan: delete the `PriceTable.fetch` line and the `prices=prices` Service kwarg; add:

```python
        from app.sandbox.maritime_agent import MaritimeAgentClient
        agent = MaritimeAgentClient(settings.maritime_api_key, client=http_maritime)
```

and pass `agent=agent` into `Services(...)`. Remove `llm=OpenRouterClient(...)` from the `Services(...)` call and delete the now-unused `OpenRouterClient` import.

`app/graph/nodes/helpers.py`: delete `model_for`, `chat_with_retry`, `apply_llm_cost` functions. `tests/test_node_helpers.py`: delete the four tests that exercised them (keep `test_conversation_id_plain_and_retry`, `test_make_services_autowires_tracker_from_redis`).

- [ ] **Step 4: Run suite**

Run: `.venv\Scripts\python.exe -m pytest tests -x -q --ignore=tests/e2e --ignore=tests/integration -k "not phase1_real"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app tests
git commit -m "feat: cost is VM-only; OpenRouter removed from pipeline path"
```

---

### Task 8: Frontend — template selector replaces model selectors

**Files:**
- Modify: `app/frontend/src/api/client.ts` (add `templates()`; remove `models()`)
- Modify: `app/frontend/src/api/types.ts` (`TaskCreateRequest.template_id: string`; remove `model_overrides`)
- Modify: `app/frontend/src/pages/CreateTaskPage.tsx`
- Test: none (manual verify via build)

**Interfaces:**
- Consumes: `GET /templates` response shape from Task 6.
- Produces: create-page form submits `{task_description, repo, base_branch, test_command, template_id}`.

- [ ] **Step 1: Update client + types**

`client.ts` — replace the `models()` method with:

```typescript
  templates: () =>
    request<{ templates: { id: string; name: string; description: string; tags: string[] }[] }>(
      "/templates",
    ).then((r) => r.templates ?? []),
```

`types.ts` — in `TaskCreateRequest`, replace `model_overrides?: Record<string, string>;` with `template_id: string;`.

- [ ] **Step 2: Rewrite CreateTaskPage model section**

Replace `MODEL_STAGES`, `stageModels`, `models`/`modelsLoading`/`modelsError` state and the `<fieldset>` with:

```typescript
const [templates, setTemplates] = useState<TemplateInfo[]>([]);
const [templatesLoading, setTemplatesLoading] = useState(true);
const [templateId, setTemplateId] = useState("codex");
```

`TemplateInfo` interface `{ id: string; name: string; description: string; tags: string[] }`; fetch via `api.templates()` in the same `useEffect`. Template options:

```typescript
const templateOptions: Option[] = useMemo(
  () => templates.map((t) => ({ value: t.id, label: t.name, hint: t.description })),
  [templates],
);
```

Fieldset replaced by a single label:

```tsx
<label>
  Agent template <span className="muted">(Maritime harness powering every stage)</span>
  <SearchableSelect
    options={templateOptions}
    value={templateId}
    onChange={setTemplateId}
    placeholder={templatesLoading ? "Loading templates…" : "Select a template…"}
    loading={templatesLoading}
  />
</label>
```

Submit body: `template_id` instead of `model_overrides` (delete the override-object construction).

- [ ] **Step 3: Build**

Run: `npm run build` (in `app\frontend`)
Expected: build succeeds, no TS errors

- [ ] **Step 4: Commit**

```bash
git add app/frontend/src
git commit -m "feat: create page selects Maritime template instead of per-stage models"
```

---

### Task 9: Restart, smoke-verify, e2e

**Files:**
- Modify: none (verification task)

**Interfaces:**
- Consumes: everything above; running Postgres/Redis (docker compose up -d).

- [ ] **Step 1: Full unit suite**

Run: `.venv\Scripts\python.exe -m pytest tests -q --ignore=tests/e2e --ignore=tests/integration -k "not phase1_real"`
Expected: PASS

- [ ] **Step 2: Restart server**

Kill the python process listening on 8000 (`Get-NetTCPConnection -LocalPort 8000 -State Listen` → OwningProcess → `Stop-Process -Id <pid> -Force`), then:

```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" -ArgumentList "run.py" `
  -RedirectStandardOutput ".server.log" -RedirectStandardError ".server.err.log" -WindowStyle Hidden
```

- [ ] **Step 3: Verify endpoints**

```powershell
(Invoke-WebRequest -Uri http://127.0.0.1:8000/templates -UseBasicParsing).Content
(Invoke-WebRequest -Uri http://127.0.0.1:8000/ -UseBasicParsing).StatusCode
```

Expected: templates JSON lists codex+dsh; 200 for the page.

- [ ] **Step 4: Real e2e on demo repo**

Create a task through the API against `demo/sample-repo` (push it to a GitHub repo first if not already available — reuse whatever repo the phase-1 e2e used; check `tests/e2e/test_phase1_real.py` for the pattern):

```powershell
$body = @{ task_description = "Add a subtract function to calc.py with tests"
           repo = "<org>/sample-repo"; base_branch = "main"
           template_id = "codex" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/tasks -ContentType "application/json" -Body $body
```

Watch `.server.log` until status reaches `awaiting_approval`; verify `GET /tasks/{id}` shows plan, code_diff, test_results populated and `model_overrides` absent; approve via `/approve` and confirm PR creation.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: maritime-harness pipeline verified end-to-end"
```
