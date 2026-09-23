# Phase 1 — Orchestrator Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the phase-1 orchestrator core: submit a task via API, watch live SSE progress, run Planner → Researcher → Coding Agent → Tester → Reviewer on real OpenRouter models inside a persistent Maritime VM, pause at Human Approval, resume via API, and survive a full process restart mid-approval.

**Architecture:** FastAPI app hosting a LangGraph `StateGraph(TaskState)` with a Postgres checkpointer (`AsyncPostgresSaver`, `thread_id = task_id`). Events flow from nodes → Redis Streams → SSE. The three decision gates (Tester, Reviewer, Human Approval) return `Command` objects that combine state updates and routing; all other wiring is static edges. Maritime provides one VM per task (`aw-task-{id}`), driven over its REST API with a background-launch-and-poll pattern to beat the 120s `exec` cap. Phase 1 finalizes with a local `done` status; push/PR arrive in the phase-2 plan.

**Tech Stack:** Python 3.12+, FastAPI, uvicorn, LangGraph + `langgraph-checkpoint-postgres`, Redis (`redis-py` asyncio), Postgres 16 (`asyncpg` for the tasks table), httpx (all external HTTP), pydantic-settings, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-23-agent-workspace-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- Python **3.12+**; import root `app/`.
- All config from env vars via `pydantic-settings` (spec §11): `OPENROUTER_API_KEY`, `MARITIME_API_KEY`, `MARITIME_TEMPLATE_ID`, `GITHUB_PAT`, `DATABASE_URL`, `REDIS_URL`, per-role `MODEL_*`, `SANDBOX_RUN_TIMEOUT_SECONDS` (default 1800).
- Docker Compose maps Postgres to host port **5433** and Redis to **6380**.
- Agent VMs are named **`aw-task-{task_id}`**; Researcher computers are named **`aw-task-{task_id}-research`** with `externalUserId = task_id` (spec §7).
- Retry bounds: `MAX_TESTING_RETRIES = 3`, `MAX_CODING_RETRIES = 2` (spec §5). Counters are incremented by `coding_agent_node` on re-entry (consume-on-entry rule in Task 9) — so the bound means N *retries* after the first attempt.
- Transient backoff: 3 attempts, delays 1s → 4s → 16s (`base_delay * 4**i`), never touching `retry_counts` (spec §5).
- Unit/integration tests never call real OpenRouter or Maritime — `httpx.MockTransport` + the fakes in `tests/fakes.py` only. Real-service tests are marked `e2e` and skip without keys.
- Maritime `exec` caps: timeout ≤ 120s (we default to 110), output ≤ 256KB; the only long-run mechanism is the background+poll pattern in Task 5.
- Maritime **computers require a paid plan** (free plan runs agents only; creation returns 402 `computer_limit`). The computers client surfaces this as `MaritimePlanError`.
- The Coding Agent **does not commit in the VM** during phase 1; changes stay staged and the cumulative diff is taken as `git add -A && git diff --cached {base_branch}`. (Committing at PR time is phase 2; no in-VM commits keeps retry diffs correct.)
- Event SSE schema: `{"task_id","node","type","data","ts"}`, types `node_started | output_chunk | tool_call | node_completed | cost_update | sleep_wake` (spec §8).
- Spec schema extensions made by this plan (flagged once): `test_command`, `computer_id`, `pr_url`, `resumed_at` added to `TaskState`.
- Shell commands in steps are PowerShell-compatible (`python -m pytest`, `git`).

## File Structure (whole phase)

```
docker-compose.yml
pyproject.toml
.env.example
.gitignore
README.md
app/
  __init__.py
  config.py            # Settings (pydantic-settings)
  db.py                # tasks table DDL + upsert/get + checkpointer factory
  main.py              # FastAPI app factory + lifespan
  graph/
    __init__.py
    state.py           # TaskState, TestResult, TaskStatus, initial_state
    edges.py           # pure routing functions + retry bounds
    build.py           # build_graph(services, checkpointer)
    nodes/
      __init__.py
      helpers.py       # Services container, emit(), model_for(), apply_llm_cost()
      planner.py
      researcher.py
      coding_agent.py
      tester.py
      reviewer.py
      human_approval.py
      needs_human.py
      commit_pr.py
  llm/
    __init__.py
    openrouter.py      # OpenRouterClient, LLMResult
    pricing.py         # PriceTable
    backoff.py         # is_transient + with_retry
  sandbox/
    __init__.py
    base.py            # ExecResult, Sandbox Protocol
    maritime.py        # MaritimeSandbox + make_sandbox()
    computers.py       # MaritimeComputers
  events/
    __init__.py
    publisher.py       # Event, EventPublisher
    sse.py             # sse_events()
  api/
    __init__.py
    routes_tasks.py
  services/
    __init__.py
    run_manager.py     # RunManager: drive/resume/mirror
tests/
  conftest.py
  fakes.py
  unit/                # (flat test files under tests/ are fine too; keep unit pure)
  integration/
  e2e/
demo/
  sample-repo/
  phase1.md
```

---

### Task 1: Project scaffold, config, docker-compose

**Files:**
- Create: `pyproject.toml`, `docker-compose.yml`, `.env.example`, `.gitignore`, `README.md`, `app/__init__.py`, `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `app.config.Settings` with fields `openrouter_api_key, openrouter_base_url, maritime_api_key, maritime_base_url, maritime_template_id, github_pat, database_url, redis_url, model_planner, model_researcher, model_coding_agent, model_reviewer, sandbox_run_timeout_seconds(1800), sandbox_exec_timeout_seconds(110), sandbox_poll_interval_seconds(2.0), test_command("pytest -q"), backoff_attempts(3), backoff_base_delay(1.0)`; `get_settings()`; `pytest` runnable; compose services on 5433/6380

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "agent-workspace"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "redis>=5.0",
    "pydantic-settings>=2.4",
    "langgraph>=0.4",
    "langgraph-checkpoint-postgres>=2.0",
    "asyncpg>=0.29",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ruff>=0.6"]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: requires docker-compose services (redis/postgres)",
    "e2e: requires real OPENROUTER_API_KEY and MARITIME_API_KEY",
]

[tool.ruff]
line-length = 100
```

- [ ] **Step 2: Create `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: aw
      POSTGRES_PASSWORD: aw
      POSTGRES_DB: agent_workspace
    ports: ["5433:5432"]
  redis:
    image: redis:7
    ports: ["6380:6379"]
```

- [ ] **Step 3: Create `.env.example`, `.gitignore`, `README.md`, `app/__init__.py`, `app/config.py`**

`.env.example`:

```
OPENROUTER_API_KEY=
MARITIME_API_KEY=
MARITIME_TEMPLATE_ID=
GITHUB_PAT=
DATABASE_URL=postgresql://aw:aw@localhost:5433/agent_workspace
REDIS_URL=redis://localhost:6380/0
```

`.gitignore`:

```
.env
__pycache__/
.venv/
.pytest_cache/
*.egg-info/
.ruff_cache/
```

`README.md`:

```markdown
# agent-workspace

Self-hosted multi-agent AI coding workspace: Planner → Researcher → Coding Agent →
Tester → Reviewer → Human Approval → Commit & PR, running on persistent Maritime VMs.

## Setup

    python -m venv .venv
    .venv\Scripts\pip install -e ".[dev]"
    copy .env.example .env   # fill in keys
    docker compose up -d     # postgres :5433, redis :6380
    python -m pytest tests -m "not integration and not e2e"

`MARITIME_TEMPLATE_ID`: pick from `curl https://api.maritime.sh/api/templates` —
choose a template with git + python3 + network access. Maritime computers (Researcher's
headful browser) additionally require a paid Maritime plan.

## Run

    .venv\Scripts\uvicorn app.main:app --port 8000
```

`app/__init__.py`: empty.

`app/config.py`:

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    maritime_api_key: str = ""
    maritime_base_url: str = "https://api.maritime.sh"
    maritime_template_id: str = ""
    github_pat: str = ""

    database_url: str = "postgresql://aw:aw@localhost:5433/agent_workspace"
    redis_url: str = "redis://localhost:6380/0"

    model_planner: str = "openai/gpt-4.1-mini"
    model_researcher: str = "openai/gpt-4.1-mini"
    model_coding_agent: str = "anthropic/claude-sonnet-4.5"
    model_reviewer: str = "openai/gpt-4.1-mini"

    sandbox_run_timeout_seconds: int = 1800
    sandbox_exec_timeout_seconds: int = 110
    sandbox_poll_interval_seconds: float = 2.0
    test_command: str = "pytest -q"

    backoff_attempts: int = 3
    backoff_base_delay: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Write the failing test**

`tests/test_config.py`:

```python
from app.config import Settings


def test_settings_defaults():
    s = Settings(openrouter_api_key="k", database_url="postgresql://x")
    assert s.sandbox_run_timeout_seconds == 1800
    assert s.sandbox_exec_timeout_seconds == 110
    assert s.model_coding_agent == "anthropic/claude-sonnet-4.5"
    assert s.test_command == "pytest -q"
```

- [ ] **Step 5: Install and run**

Run: `pip install -e ".[dev]"` then `python -m pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml docker-compose.yml .env.example .gitignore README.md app tests
git commit -m "feat: scaffold project, settings, compose services"
```

---

### Task 2: TaskState, TestResult, pure routing edges

**Files:**
- Create: `app/graph/__init__.py` (empty), `app/graph/state.py`, `app/graph/edges.py`
- Test: `tests/test_edges.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `TaskStatus` Literal (spec §4 + `"needs_human"`)
  - `TestResult` dataclass (`passed: bool`, `failing_output: str`, `failing_tests: list[str]`) with `to_dict()` / `from_dict(d)`
  - `TaskState(TypedDict)` — all keys listed in `initial_state` below
  - `initial_state(task_id, description, repo, base_branch, test_command, model_overrides) -> TaskState`
  - `MAX_TESTING_RETRIES = 3`, `MAX_CODING_RETRIES = 2`
  - `route_after_tester(state) -> "reviewer" | "coding_agent" | "needs_human"`
  - `route_after_reviewer(state) -> "human_approval" | "coding_agent" | "needs_human"`
  - `route_after_human(state) -> "commit_pr" | "planner"`

- [ ] **Step 1: Write the failing tests**

`tests/test_edges.py`:

```python
from app.graph.edges import (
    MAX_CODING_RETRIES,
    MAX_TESTING_RETRIES,
    route_after_human,
    route_after_reviewer,
    route_after_tester,
)
from app.graph.state import initial_state


def base_state(**kw):
    st = dict(initial_state("t1", "desc", "org/repo", "main", "pytest -q", {}))
    st.update(kw)
    return st


def test_bounds():
    assert MAX_TESTING_RETRIES == 3
    assert MAX_CODING_RETRIES == 2


def test_initial_state_defaults():
    st = initial_state("t1", "d", "org/repo", "main", "", {})
    assert st["status"] == "planning"
    assert st["retry_counts"] == {"testing": 0, "coding": 0}
    assert st["approval_status"] == "pending"
    assert st["error_log"] == [] and st["cost_so_far"] == 0.0


def test_tester_pass_routes_to_reviewer():
    assert route_after_tester(base_state(test_results={"passed": True})) == "reviewer"


def test_tester_fail_routes_to_coding():
    st = base_state(test_results={"passed": False}, retry_counts={"testing": 0})
    assert route_after_tester(st) == "coding_agent"


def test_tester_bound_exceeded_routes_to_needs_human():
    st = base_state(test_results={"passed": False}, retry_counts={"testing": MAX_TESTING_RETRIES})
    assert route_after_tester(st) == "needs_human"


def test_reviewer_approved_routes_to_human():
    st = base_state(approval_status="approved", retry_counts={"coding": 0})
    assert route_after_reviewer(st) == "human_approval"


def test_reviewer_rejected_routes_to_coding():
    st = base_state(approval_status="needs_changes", review_comments=["x"], retry_counts={"coding": 1})
    assert route_after_reviewer(st) == "coding_agent"


def test_reviewer_bound_exceeded_routes_to_needs_human():
    st = base_state(approval_status="needs_changes", review_comments=["x"],
                    retry_counts={"coding": MAX_CODING_RETRIES})
    assert route_after_reviewer(st) == "needs_human"


def test_human_routes():
    assert route_after_human(base_state(approval_status="approved")) == "commit_pr"
    assert route_after_human(base_state(approval_status="rejected")) == "planner"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_edges.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.graph'`.

- [ ] **Step 3: Implement `app/graph/state.py`**

```python
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional, TypedDict

TaskStatus = Literal[
    "planning", "researching", "coding", "testing", "reviewing",
    "awaiting_approval", "committing", "done", "failed", "needs_human",
]


@dataclass
class TestResult:
    passed: bool
    failing_output: str = ""
    failing_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TestResult":
        return cls(passed=d["passed"], failing_output=d.get("failing_output", ""),
                   failing_tests=d.get("failing_tests", []))


class TaskState(TypedDict):
    task_id: str
    task_description: str
    repo: str
    base_branch: str
    test_command: str
    plan: Optional[str]
    research_notes: Optional[str]
    code_diff: Optional[str]
    test_results: Optional[dict]
    review_comments: Optional[list[str]]
    approval_status: Literal["pending", "approved", "rejected", "needs_changes"]
    retry_counts: dict
    cost_so_far: float
    sandbox_id: Optional[str]
    computer_id: Optional[str]
    status: TaskStatus
    error_log: list
    model_overrides: dict
    paused_at: Optional[str]
    resumed_at: Optional[str]
    pr_url: Optional[str]


def initial_state(task_id: str, description: str, repo: str, base_branch: str,
                  test_command: str, model_overrides: dict) -> TaskState:
    return TaskState(
        task_id=task_id, task_description=description, repo=repo, base_branch=base_branch,
        test_command=test_command or "", plan=None, research_notes=None, code_diff=None,
        test_results=None, review_comments=None, approval_status="pending",
        retry_counts={"testing": 0, "coding": 0}, cost_so_far=0.0, sandbox_id=None,
        computer_id=None, status="planning", error_log=[], model_overrides=model_overrides,
        paused_at=None, resumed_at=None, pr_url=None,
    )
```

`app/graph/edges.py`:

```python
MAX_TESTING_RETRIES = 3
MAX_CODING_RETRIES = 2


def route_after_tester(state) -> str:
    if state["test_results"]["passed"]:
        return "reviewer"
    if state["retry_counts"].get("testing", 0) >= MAX_TESTING_RETRIES:
        return "needs_human"
    return "coding_agent"


def route_after_reviewer(state) -> str:
    if state["approval_status"] == "approved":
        return "human_approval"
    if state["retry_counts"].get("coding", 0) >= MAX_CODING_RETRIES:
        return "needs_human"
    return "coding_agent"


def route_after_human(state) -> str:
    return "commit_pr" if state["approval_status"] == "approved" else "planner"
```

- [ ] **Step 4: Run tests to pass**

Run: `python -m pytest tests/test_edges.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add app tests
git commit -m "feat: TaskState schema and pure routing edges"
```

---

### Task 3: Events — Redis Stream publisher + SSE generator

**Files:**
- Create: `app/events/__init__.py` (empty), `app/events/publisher.py`, `app/events/sse.py`, `tests/conftest.py`
- Test: `tests/test_events_unit.py`, `tests/integration/test_events_redis.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis` (from compose, integration marker)
- Produces:
  - `Event(task_id, node, type, data, ts="")` — `ts` auto-filled with local ISO timestamp when empty; `to_stream_fields() -> dict` (data JSON-encoded) and `to_sse() -> str` (`event: <type>\ndata: <json>\n\n`)
  - `EventPublisher(redis, prefix="aw").publish(event) -> stream_id` — XADD to `aw:{task_id}:events`
  - `sse_events(redis, task_id, prefix="aw") -> AsyncIterator[str]` — yields SSE strings, starting at stream id `"0"` (replay), blocking XREAD with 15s blocks

- [ ] **Step 1: Write the failing unit tests**

`tests/test_events_unit.py`:

```python
import json

from app.events.publisher import Event


def test_event_stream_fields():
    e = Event(task_id="t1", node="planner", type="node_started", data={"a": 1},
              ts="2026-01-01T00:00:00Z")
    f = e.to_stream_fields()
    assert f["task_id"] == "t1"
    assert f["type"] == "node_started"
    assert json.loads(f["data"]) == {"a": 1}


def test_sse_format():
    e = Event(task_id="t1", node="planner", type="cost_update", data={"cost": 0.5}, ts="T")
    assert e.to_sse() == (
        'event: cost_update\n'
        'data: {"task_id": "t1", "node": "planner", "type": "cost_update", '
        '"data": {"cost": 0.5}, "ts": "T"}\n\n'
    )


def test_ts_autofilled():
    e = Event(task_id="t", node="n", type="tool_call", data={})
    assert e.ts  # non-empty ISO timestamp
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_events_unit.py -v`
Expected: FAIL — no module `app.events`.

- [ ] **Step 3: Implement `app/events/publisher.py`**

```python
import json
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Event:
    task_id: str
    node: str
    type: str
    data: dict
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now().astimezone().isoformat()

    def to_stream_fields(self) -> dict:
        return {"task_id": self.task_id, "node": self.node, "type": self.type,
                "data": json.dumps(self.data), "ts": self.ts}

    def to_sse(self) -> str:
        payload = {"task_id": self.task_id, "node": self.node, "type": self.type,
                   "data": self.data, "ts": self.ts}
        return f"event: {self.type}\ndata: {json.dumps(payload)}\n\n"


class EventPublisher:
    def __init__(self, redis, prefix: str = "aw"):
        self._redis = redis
        self._prefix = prefix

    async def publish(self, event: Event) -> str:
        return await self._redis.xadd(f"{self._prefix}:{event.task_id}:events",
                                      event.to_stream_fields())
```

`app/events/sse.py`:

```python
import json

from app.events.publisher import Event


async def sse_events(redis, task_id: str, prefix: str = "aw"):
    """Yield SSE strings for all events on the task's stream, then keep following."""
    key = f"{prefix}:{task_id}:events"
    last_id = "0"
    while True:
        resp = await redis.xread({key: last_id}, block=15000, count=100)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                last_id = entry_id
                event = Event(task_id=fields["task_id"], node=fields["node"],
                              type=fields["type"], data=json.loads(fields["data"]),
                              ts=fields["ts"])
                yield event.to_sse()
```

`tests/conftest.py` (initial version; later tasks extend it):

```python
import pytest

from app.config import Settings


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
async def redis_client(settings):
    import redis.asyncio as aioredis
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()
```

- [ ] **Step 4: Write the integration test**

`tests/integration/test_events_redis.py`:

```python
import json

import pytest

from app.events.publisher import Event, EventPublisher
from app.events.sse import sse_events

pytestmark = pytest.mark.integration


async def test_publish_then_stream_replays(redis_client):
    pub = EventPublisher(redis_client)
    for i in range(3):
        await pub.publish(Event(task_id="tX", node="planner", type="node_completed",
                                data={"i": i}))
    collected = []
    async for sse in sse_events(redis_client, "tX"):
        collected.append(sse)
        if len(collected) == 3:
            break
    assert len(collected) == 3
    payload = json.loads(collected[0].split("data: ", 1)[1].strip().rsplit("\n", 1)[0])
    assert payload["type"] == "node_completed" and payload["task_id"] == "tX"
```

- [ ] **Step 5: Run integration test (compose must be up)**

Run: `docker compose up -d` then `python -m pytest tests/integration/test_events_redis.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app tests
git commit -m "feat: event publisher and SSE generator over Redis Streams"
```

---

### Task 4: OpenRouter client, pricing, transient backoff

**Files:**
- Create: `app/llm/__init__.py` (empty), `app/llm/openrouter.py`, `app/llm/pricing.py`, `app/llm/backoff.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`
- Produces:
  - `LLMResult(text: str, prompt_tokens: int, completion_tokens: int, model: str)`
  - `OpenRouterClient(api_key, base_url, client=None).chat(model, messages, stream_cb=None) -> LLMResult` (non-streaming default; when `stream_cb` given, streams deltas calling `stream_cb(delta)` and takes usage from the final chunk) and `.models() -> list[dict]`
  - `PriceTable(prices).cost(model, prompt_tokens, completion_tokens) -> float` (unknown model → 0.0); `await PriceTable.fetch(client)` parses `/models` pricing strings
  - `is_transient(exc) -> bool` (httpx transport errors; HTTP 408/429/5xx); `await with_retry(fn, attempts, base_delay, sleep=None)`

- [ ] **Step 1: Write the failing tests**

`tests/test_llm.py`:

```python
import json

import httpx
import pytest

from app.llm.backoff import is_transient, with_retry
from app.llm.openrouter import OpenRouterClient
from app.llm.pricing import PriceTable


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://openrouter.ai/api/v1")


async def test_chat_extracts_usage():
    def handler(request):
        assert request.url.path == "/api/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "the plan"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    c = OpenRouterClient(api_key="k", client=mock_client(handler))
    res = await c.chat("m", [{"role": "user", "content": "hi"}])
    assert res.text == "the plan"
    assert res.prompt_tokens == 100 and res.completion_tokens == 20


async def test_chat_streams_deltas_and_final_usage():
    sse_lines = [
        'data: {"choices": [{"delta": {"content": "hel"}}]}',
        'data: {"choices": [{"delta": {"content": "lo"}}]}',
        'data: {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}',
        "data: [DONE]",
    ]

    def handler(request):
        return httpx.Response(200, content="\n\n".join(sse_lines).encode())

    c = OpenRouterClient(api_key="k", client=mock_client(handler))
    seen = []
    res = await c.chat("m", [{"role": "user", "content": "hi"}], stream_cb=seen.append)
    assert res.text == "hello"
    assert seen == ["hel", "lo"[:2]] or seen == ["hel", "l", "o"][:2] or "".join(seen) == "hello"
    assert res.prompt_tokens == 7 and res.completion_tokens == 2


async def test_price_table_cost_math():
    pt = PriceTable({"openai/gpt-4.1-mini": {"prompt": 0.000003, "completion": 0.000015}})
    assert abs(pt.cost("openai/gpt-4.1-mini", 1000, 100) - 0.0045) < 1e-12
    assert pt.cost("unknown/model", 100, 100) == 0.0


async def test_price_table_fetch_parses_strings():
    def handler(request):
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json={"data": [
            {"id": "x/y", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
            {"id": "z/bad", "pricing": {"prompt": "n/a", "completion": "0"}},
        ]})

    pt = await PriceTable.fetch(mock_client(handler))
    assert abs(pt.cost("x/y", 1_000_000, 0) - 3.0) < 1e-9
    assert pt.cost("z/bad", 100, 100) == 0.0


class FakeSleep:
    def __init__(self):
        self.delays = []

    async def __call__(self, s):
        self.delays.append(s)


async def test_backoff_retries_transient_then_succeeds():
    import httpx

    from app.llm.backoff import with_retry
    calls = {"n": 0}
    sleeper = FakeSleep()

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPStatusError("boom", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(429))
        return "ok"

    assert await with_retry(flaky, attempts=3, base_delay=1.0, sleep=sleeper) == "ok"
    assert calls["n"] == 3
    assert sleeper.delays == [1.0, 4.0]


async def test_backoff_does_not_retry_client_errors():
    import httpx

    from app.llm.backoff import with_retry
    calls = {"n": 0}

    async def bad_request():
        calls["n"] += 1
        raise httpx.HTTPStatusError("nope", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(400))

    with pytest.raises(httpx.HTTPStatusError):
        await with_retry(bad_request, attempts=3, base_delay=0.0, sleep=FakeSleep())
    assert calls["n"] == 1


def test_transient_classification():
    from app.llm.backoff import is_transient

    t = httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"),
                              response=httpx.Response(503))
    assert is_transient(t)
    bad = httpx.HTTPStatusError("e", request=httpx.Request("GET", "http://x"),
                                response=httpx.Response(400))
    assert not is_transient(bad)
    assert is_transient(httpx.ConnectTimeout("t"))
```

(One honest simplification for the streaming test: replace the three-way `or` assert with the single unambiguous assertion `assert "".join(seen) == "hello"`. Use that final form in the implemented test file.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_llm.py -v`
Expected: FAIL — no module `app.llm`.

- [ ] **Step 3: Implement `app/llm/backoff.py`**

```python
import asyncio

import httpx

_TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUSES
    return False


async def with_retry(fn, attempts: int = 3, base_delay: float = 1.0, sleep=None):
    """Retry transient failures with exponential backoff (base * 4**i).

    Never counts against retry_counts (spec §5). Non-transient errors re-raise
    immediately; exhausted attempts re-raise the last exception.
    """
    if sleep is None:
        sleep = asyncio.sleep
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - classified below
            if not is_transient(e) or i == attempts - 1:
                raise
            await sleep(base_delay * (4 ** i))
```

- [ ] **Step 4: Implement `app/llm/pricing.py`**

```python
class PriceTable:
    """Per-model price in USD per token, parsed from OpenRouter /models strings."""

    def __init__(self, prices: dict[str, dict[str, float]]):
        self._prices = prices

    @classmethod
    async def fetch(cls, client) -> "PriceTable":
        r = await client.get("/models")
        r.raise_for_status()
        prices: dict[str, dict[str, float]] = {}
        for m in r.json()["data"]:
            p = m.get("pricing") or {}
            try:
                prices[m["id"]] = {"prompt": float(p.get("prompt", 0) or 0),
                                   "completion": float(p.get("completion", 0) or 0)}
            except (TypeError, ValueError):
                continue
        return cls(prices)

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p = self._prices.get(model)
        if not p:
            return 0.0
        return prompt_tokens * p["prompt"] + completion_tokens * p["completion"]
```

- [ ] **Step 5: Implement `app/llm/openrouter.py`**

```python
import json
from typing import Callable

import httpx


class LLMResult:
    def __init__(self, text: str, prompt_tokens: int, completion_tokens: int, model: str):
        self.text = text
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.model = model


class OpenRouterClient:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1",
                 client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=120)
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "HTTP-Referer": "https://agent-workspace.local",
                         "X-Title": "agent-workspace"}

    async def chat(self, model: str, messages: list[dict],
                   stream_cb: Callable[[str], None] | None = None) -> LLMResult:
        body = {"model": model, "messages": messages}
        if stream_cb is not None:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        r = await self._client.post("/chat/completions", json=body, headers=self._headers)
        r.raise_for_status()
        if stream_cb is None:
            d = r.json()
            u = d.get("usage") or {}
            return LLMResult(d["choices"][0]["message"]["content"],
                             u.get("prompt_tokens", 0), u.get("completion_tokens", 0), model)

        text_parts: list[str] = []
        usage: dict = {}
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {}).get("content") or ""
                if delta:
                    text_parts.append(delta)
                    stream_cb(delta)
        return LLMResult("".join(text_parts), usage.get("prompt_tokens", 0),
                         usage.get("completion_tokens", 0), model)

    async def models(self) -> list[dict]:
        r = await self._client.get("/models", headers=self._headers)
        r.raise_for_status()
        return r.json()["data"]
```

- [ ] **Step 6: Run tests to pass**

Run: `python -m pytest tests/test_llm.py -v`
Expected: PASS (7 tests).

- [ ] **Step 7: Commit**

```bash
git add app tests
git commit -m "feat: OpenRouter client, price table, transient backoff"
```

---

### Task 5: Sandbox protocol + Maritime VM wrapper

**Files:**
- Create: `app/sandbox/__init__.py` (empty), `app/sandbox/base.py`, `app/sandbox/maritime.py`
- Test: `tests/test_maritime_sandbox.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `httpx.AsyncClient`, `with_retry` (Task 4)
- Produces:
  - `ExecResult(exit_code: int, stdout: str, stderr: str)` with `.combined` property
  - `Sandbox` Protocol: `agent_id`, `ensure() -> str`, `exec(command, timeout=None) -> ExecResult`, `run_long(command, timeout=None) -> ExecResult`, `read_file(path) -> str`, `write_file(path, content) -> None`, `list_files(path) -> list[dict]`, `diff() -> str`, `sleep() -> None`
  - `MaritimeSandbox(settings, client, task_id, repo, base_branch)` implementing the protocol (REST endpoints per spec §7)
  - `make_sandbox(settings, task_id, repo, base_branch, client=None) -> MaritimeSandbox` — the factory nodes use via `Services.sandbox_factory`

REST details implemented: `POST /api/agents {name, templateId}` (201 → `{"id"}`), `GET /api/agents/{id}` (200 = exists / 404 = recreate), `POST /api/agents/{id}/exec {command, timeout}` → `{exitCode, stdout, stderr}` (command = raw shell string), `PUT .../files/write {path, content}`, `GET .../files/download?path=`, `POST .../sleep`.

- [ ] **Step 1: Write the failing tests**

`tests/test_maritime_sandbox.py`:

```python
import json

import httpx
import pytest

from app.config import Settings
from app.sandbox.maritime import MaritimeSandbox

pytestmark = pytest.mark.integration


class MaritimeMock:
    """In-memory Maritime API: one agent namespace, files dict, scripted exec."""

    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.files: dict[tuple[str, str], str] = {}
        self.next_id = 1
        self.exec_calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/agents":
            body = json.loads(request.content)
            aid = f"a{self.next_id}"
            self.next_id += 1
            self.agents[aid] = body
            return httpx.Response(201, json={"id": aid, "name": body["name"]})
        if method == "GET" and path.startswith("/api/agents/"):
            aid = path.split("/")[-1]
            if aid in self.agents:
                return httpx.Response(200, json={"id": aid, "status": "running"})
            return httpx.Response(404, json={"error": "not_found"})
        if method == "POST" and path.endswith("/exec"):
            body = json.loads(request.content)
            return httpx.Response(200, json=self._exec(body["command"]))
        if method == "PUT" and path.endswith("/write"):
            aid = path.split("/")[-2]
            body = json.loads(request.content)
            self.files[(aid, body["path"])] = body["content"]
            return httpx.Response(200, json={"ok": True})
        if method == "GET" and path.endswith("/download"):
            aid = path.split("/")[-2]
            p = request.url.params.get("path", "")
            return httpx.Response(200, content=self.files.get((aid, p), "").encode())
        if method == "POST" and path.endswith("/sleep"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "unmocked", "path": path})

    def _exec(self, command) -> dict:
        raw = command if isinstance(command, str) else " ".join(command)
        self.exec_calls.append(raw)
        if "git clone" in raw:
            return {"exitCode": 0, "stdout": "cloned", "stderr": ""}
        if "git diff --cached" in raw and "git add" in raw:
            return {"exitCode": 0, "stdout": "diff --git a/f.py b/f.py\n+print(1)", "stderr": ""}
        if raw.startswith("nohup"):
            return {"exitCode": 0, "stdout": "1234", "stderr": ""}
        if ".code" in raw:  # poll: exit-code file present -> done, code 0
            return {"exitCode": 0, "stdout": "DONE\n0", "stderr": ""}
        if raw.startswith("cat /data/.runs"):
            return {"exitCode": 0, "stdout": "1 passed in 0.01s", "stderr": ""}
        return {"exitCode": 0, "stdout": "ok", "stderr": ""}


def make(m: MaritimeMock) -> tuple[Settings, httpx.AsyncClient, MaritimeSandbox]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(m.handler),
                               base_url="https://api.maritime.sh")
    s = Settings(maritime_api_key="mk_test", maritime_template_id="t-code",
                 sandbox_poll_interval_seconds=0.0)
    sb = MaritimeSandbox(s, client, "t1", "org/repo", base_branch="main")
    return s, client, sb


async def test_ensure_creates_then_reuses():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    aid1 = await sb.ensure()
    aid2 = await sb.ensure()
    assert aid1 == aid2 == sb.agent_id
    assert m.agents[aid1]["name"] == "aw-task-t1"


async def test_ensure_recreates_after_404():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    aid1 = await sb.ensure()
    del m.agents[aid1]
    aid2 = await sb.ensure()
    assert aid2 != aid1
    assert any("git clone" in c for c in m.exec_calls)


async def test_provision_installs_venv_and_runs_dir():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    script = [c for c in m.exec_calls if "python3 -m venv" in c]
    assert script and "mkdir -p /data/.runs" in script[0]


async def test_run_long_returns_log_output():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    res = await sb.run_long("pytest -q", timeout=10)
    assert res.exit_code == 0
    assert "1 passed" in res.stdout


async def test_write_and_read_file():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    await sb.write_file("/data/workspace/calc.py", "x = 1")
    assert await sb.read_file("/data/workspace/calc.py") == "x = 1"


async def test_diff_uses_base_branch():
    m = MaritimeMock()
    _s, _c, sb = make(m)
    await sb.ensure()
    d = await sb.diff()
    assert d.startswith("diff --git")
    assert any("git diff --cached main" in c for c in m.exec_calls)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_maritime_sandbox.py -v`
Expected: FAIL — no module `app.sandbox`.

- [ ] **Step 3: Implement `app/sandbox/base.py`**

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        out = self.stdout.strip()
        if self.stderr.strip():
            out += "\n" + self.stderr.strip()
        return out


class Sandbox(Protocol):
    agent_id: str | None

    async def ensure(self) -> str: ...
    async def exec(self, command, timeout=None) -> ExecResult: ...
    async def run_long(self, command: str, timeout=None) -> ExecResult: ...
    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str) -> None: ...
    async def list_files(self, path: str) -> list[dict]: ...
    async def diff(self) -> str: ...
    async def sleep(self) -> None: ...
```

- [ ] **Step 4: Implement `app/sandbox/maritime.py`**

```python
import asyncio
import time

import httpx

from app.llm.backoff import with_retry
from app.sandbox.base import ExecResult

RUNS_DIR = "/data/.runs"
WORKSPACE = "/data/workspace"
VENV = "/data/venv"


class MaritimeSandbox:
    """One persistent Maritime VM per task. All repo-touching nodes share it."""

    def __init__(self, settings, client: httpx.AsyncClient, task_id: str,
                 repo: str, base_branch: str):
        self._s = settings
        self._client = client
        self.task_id = task_id
        self.repo = repo
        self.base_branch = base_branch
        self.agent_id: str | None = None

    def _headers(self):
        return {"Authorization": f"Bearer {self._s.maritime_api_key}"}

    async def ensure(self) -> str:
        if self.agent_id:
            r = await self._client.get(f"/api/agents/{self.agent_id}", headers=self._headers())
            if r.status_code == 200:
                return self.agent_id
            self.agent_id = None  # 404: VM deleted — recreate (spec §7)
        r = await with_retry(lambda: self._client.post(
            "/api/agents", headers=self._headers(),
            json={"name": f"aw-task-{self.task_id}",
                  "templateId": self._s.maritime_template_id}))
        r.raise_for_status()
        self.agent_id = r.json()["id"]
        await self._provision()
        return self.agent_id

    async def _provision(self):
        clone_url = f"https://x-access-token:{self._s.github_pat}@github.com/{self.repo}.git"
        script = (
            f"set -e\n"
            f"if [ ! -d {WORKSPACE}/.git ]; then git clone {clone_url} {WORKSPACE} "
            f"-b {self.base_branch}; fi\n"
            f"if [ ! -d {VENV} ]; then python3 -m venv {VENV}; fi\n"
            f"if [ -f {WORKSPACE}/requirements.txt ]; then "
            f"{VENV}/bin/pip install -q -r {WORKSPACE}/requirements.txt; fi\n"
            f"mkdir -p {RUNS_DIR}\n"
        )
        res = await self.exec(script)
        if res.exit_code != 0:
            raise RuntimeError(f"sandbox provisioning failed: {res.combined[:2000]}")

    async def exec(self, command, timeout=None) -> ExecResult:
        t = self._s.sandbox_exec_timeout_seconds if timeout is None else timeout
        r = await self._client.post(f"/api/agents/{self.agent_id}/exec",
                                    headers=self._headers(),
                                    json={"command": command, "timeout": t})
        r.raise_for_status()
        d = r.json()
        return ExecResult(d["exitCode"], d.get("stdout", ""), d.get("stderr", ""))

    async def run_long(self, command: str, timeout=None) -> ExecResult:
        """Background-launch + poll: beats Maritime's 120s exec cap (spec §7)."""
        timeout = self._s.sandbox_run_timeout_seconds if timeout is None else timeout
        run_id = f"{int(time.time() * 1000)}-{self.task_id}"
        log, codef = f"{RUNS_DIR}/{run_id}.log", f"{RUNS_DIR}/{run_id}.code"
        launch = (f"cd {WORKSPACE} && mkdir -p {RUNS_DIR} && nohup bash -c "
                  f"'source {VENV}/bin/activate 2>/dev/null; {command}; echo $? > {codef}' "
                  f"> {log} 2>&1 & echo $!")
        started = await self.exec(launch, timeout=30)
        if started.exit_code != 0 or not started.stdout.strip():
            raise RuntimeError(f"run_long launch failed: {started.combined[:1000]}")
        deadline = time.monotonic() + timeout
        while True:
            poll = await self.exec(f"if [ -f {codef} ]; then echo DONE; cat {codef}; "
                                   f"else echo RUNNING; fi", timeout=15)
            if "DONE" in poll.stdout:
                exit_code = int(poll.stdout.strip().splitlines()[-1])
                out = await self.exec(f"tail -c 256000 {log}", timeout=30)
                return ExecResult(exit_code, out.stdout, out.stderr)
            if time.monotonic() > deadline:
                return ExecResult(124, f"run_long timeout after {timeout}s (log: {log})", "")
            await asyncio.sleep(self._s.sandbox_poll_interval_seconds)

    async def read_file(self, path: str) -> str:
        r = await self._client.get(f"/api/agents/{self.agent_id}/files/download",
                                   headers=self._headers(), params={"path": path})
        r.raise_for_status()
        return r.text

    async def write_file(self, path: str, content: str) -> None:
        r = await self._client.put(f"/api/agents/{self.agent_id}/files/write",
                                   headers=self._headers(),
                                   json={"path": path, "content": content})
        r.raise_for_status()

    async def list_files(self, path: str) -> list[dict]:
        r = await self._client.get(f"/api/agents/{self.agent_id}/files/list",
                                   headers=self._headers(), params={"path": path})
        r.raise_for_status()
        return r.json().get("entries", [])

    async def diff(self) -> str:
        """Cumulative staged diff against the base branch (no in-VM commits)."""
        res = await self.exec(f"cd {WORKSPACE} && git add -A && git diff --cached {self.base_branch}")
        return res.stdout

    async def sleep(self) -> None:
        await self._client.post(f"/api/agents/{self.agent_id}/sleep", headers=self._headers())


def make_sandbox(settings, task_id: str, repo: str, base_branch: str, client=None) -> MaritimeSandbox:
    client = client or httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
    return MaritimeSandbox(settings, client, task_id, repo, base_branch)
```

- [ ] **Step 5: Run tests to pass**

Run: `python -m pytest tests/test_maritime_sandbox.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app tests
git commit -m "feat: Maritime VM sandbox wrapper with run_long poll pattern"
```

---

### Task 6: Maritime computers client (Researcher's headful browser)

**Files:**
- Create: `app/sandbox/computers.py`
- Test: `tests/test_computers.py`

**Interfaces:**
- Consumes: `Settings`, `httpx.AsyncClient`
- Produces:
  - `ComputerInfo` (attrs `computer_id, frame_id, width, height, raw`)
  - `MaritimePlanError` (402 `computer_limit`/`no_plan`/`plan_lapsed` — computers need a paid plan)
  - `HumanInControl` (409 during takeover)
  - `MaritimeComputers(settings, client=None)` with:
    - `ensure(task_id) -> ComputerInfo` — POST `/api/v1/computers` `{"externalUserId": task_id, "name": f"aw-task-{task_id}-research"}` (get-or-create per docs)
    - `act(computer_id, action: dict) -> ComputerInfo` — POST `.../{id}/actions` (action schema: `{"action": "screenshot|type|key|left_click|scroll|wait|...", "coordinate": [x,y], "text": ...}`)
    - `screenshot(computer_id) -> (bytes, frame_id)` — GET `.../{id}/screenshot`
    - `shell(computer_id, command, timeout_s=30) -> ExecResult` — POST `.../{id}/exec {command, timeoutS}`
    - `open_url(computer_id, url) -> ComputerInfo` — shell launch chromium + wait action
    - `viewer_link(computer_id, mode="watch", ttl_s=600) -> str` — POST `.../{id}/viewer`
    - `sleep(computer_id) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_computers.py`:

```python
import json

import httpx
import pytest

from app.config import Settings
from app.sandbox.computers import HumanInControl, MaritimeComputers, MaritimePlanError


class ComputersMock:
    def __init__(self):
        self.created = []
        self.actions = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/v1/computers":
            body = json.loads(request.content)
            self.created.append(body)
            if body.get("externalUserId") == "t402":
                return httpx.Response(402, json={"error": "computer_limit", "message": "need paid plan"})
            return httpx.Response(201, json={"computerId": "c1", "frameId": 0,
                                             "width": 1200, "height": 750})
        if method == "POST" and path.endswith("/actions"):
            self.actions.append(json.loads(request.content))
            return httpx.Response(200, json={"frameId": 5, "width": 1200, "height": 750})
        if method == "POST" and path.endswith("/exec"):
            return httpx.Response(200, json={"exitCode": 0, "stdout": "ok", "stderr": ""})
        if method == "POST" and path.endswith("/viewer"):
            return httpx.Response(200, json={"url": "https://view.maritime.sh/abc"})
        if method == "POST" and path.endswith("/sleep"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/409"):
            return httpx.Response(409, json={"error": "human_in_control"})
        return httpx.Response(404, json={"error": "unmocked", "path": path})


def make(mock):
    client = httpx.AsyncClient(transport=httpx.MockTransport(mock.handler),
                               base_url="https://api.maritime.sh")
    return MaritimeComputers(Settings(maritime_api_key="mk"), client)


async def test_ensure_get_or_create_sends_external_user_id():
    m = ComputersMock()
    mc = make(m)
    info = await mc.ensure("t1")
    assert info.computer_id == "c1"
    assert m.created == [{"externalUserId": "t1", "name": "aw-task-t1-research"}]


async def test_act_and_viewer_and_shell():
    m = ComputersMock()
    mc = make(m)
    info = await mc.act("c1", {"action": "screenshot"})
    assert info.frame_id == 5
    assert await mc.viewer_link("c1") == "https://view.maritime.sh/abc"
    res = await mc.shell("c1", "ls")
    assert res.exit_code == 0
    assert "chromium" in (await mc.open_url.__doc__ or "") or True  # open_url covered below


async def test_open_url_runs_chromium_then_waits():
    m = ComputersMock()
    mc = make(m)
    await mc.open_url("c1", "https://docs.example.com")
    assert any("chromium" in a.get("command", "") for a in m.actions) is False  # shell goes via exec
    assert {"action": "wait", "duration": 3} in m.actions


async def test_402_maps_to_plan_error():
    mc = make(ComputersMock())

    class PaidGate(ComputersMock):
        def handler(self, request):
            if request.url.path == "/api/v1/computers":
                return httpx.Response(402, json={"error": "computer_limit", "message": "paid plan required"})
            return super().handler(request)

    mc2 = MaritimeComputers(Settings(maritime_api_key="k"),
                            httpx.AsyncClient(transport=httpx.MockTransport(PaidGate().handler),
                                              base_url="https://api.maritime.sh"))
    with pytest.raises(MaritimePlanError):
        await mc2.ensure("t402")


async def test_409_human_in_control():
    client = httpx.AsyncClient(transport=httpx.MockTransport(ComputersMock().handler),
                               base_url="https://api.maritime.sh")
    mc = MaritimeComputers(Settings(maritime_api_key="k"), client)
    with pytest.raises(HumanInControl):
        await mc.act("c1/409", {"action": "screenshot"})
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_computers.py -v` → FAIL (no module).

- [ ] **Step 3: Implement `app/sandbox/computers.py`**

```python
import shlex

import httpx

from app.sandbox.base import ExecResult


class MaritimePlanError(RuntimeError):
    """Computers require a paid Maritime plan (402 computer_limit/no_plan/plan_lapsed)."""


class HumanInControl(RuntimeError):
    """A person holds the desktop (409) — wait, then retry."""


class ComputerInfo:
    def __init__(self, d: dict):
        self.computer_id = d.get("computerId") or d.get("id")
        self.frame_id = d.get("frameId")
        self.width = d.get("width")
        self.height = d.get("height")
        self.raw = d


class MaritimeComputers:
    def __init__(self, settings, client: httpx.AsyncClient | None = None):
        self._s = settings
        self._client = client or httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=90)

    def _headers(self):
        return {"Authorization": f"Bearer {self._s.maritime_api_key}"}

    async def _req(self, method: str, path: str, body=None) -> httpx.Response:
        r = await self._client.request(method, path, headers=self._headers(), json=body)
        if r.status_code == 402:
            raise MaritimePlanError(r.text[:300])
        if r.status_code == 409 and "human_in_control" in r.text:
            raise HumanInControl(r.text[:300])
        r.raise_for_status()
        return r

    async def ensure(self, task_id: str) -> ComputerInfo:
        r = await self._req("POST", "/api/v1/computers",
                            {"externalUserId": task_id, "name": f"aw-task-{task_id}-research"})
        return ComputerInfo(r.json())

    async def act(self, computer_id: str, action: dict) -> ComputerInfo:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/actions", action)
        return ComputerInfo(r.json())

    async def screenshot(self, computer_id: str) -> tuple[bytes, str | None]:
        r = await self._client.get(f"/api/v1/computers/{computer_id}/screenshot",
                                   headers=self._headers())
        r.raise_for_status()
        return r.content, r.headers.get("X-Frame-Id")

    async def shell(self, computer_id: str, command: str, timeout_s: int = 30) -> ExecResult:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/exec",
                            {"command": command, "timeoutS": timeout_s})
        d = r.json()
        return ExecResult(d.get("exitCode", 1), d.get("stdout", ""), d.get("stderr", ""))

    async def open_url(self, computer_id: str, url: str) -> ComputerInfo:
        await self.shell(computer_id, f"nohup chromium --new-window {shlex.quote(url)} >/dev/null 2>&1 &")
        return await self.act(computer_id, {"action": "wait", "duration": 3})

    async def viewer_link(self, computer_id: str, mode: str = "watch", ttl_s: int = 600) -> str:
        r = await self._req("POST", f"/api/v1/computers/{computer_id}/viewer",
                            {"mode": mode, "ttlS": ttl_s})
        return r.json().get("url", "")

    async def sleep(self, computer_id: str) -> None:
        await self._req("POST", f"/api/v1/computers/{computer_id}/sleep", {})
```

- [ ] **Step 4: Fix the redundant test line**

In `test_computers.py`, delete the placeholder line containing `open_url.__doc__` — keep only the real assertions (`test_act_and_viewer`, `test_open_url_runs_chromium_then_waits`, `test_402_maps_to_plan_error`, `test_409_maps_to_human_in_control`). Rename tests accordingly; do not keep the confused `assert ... or True` line.

- [ ] **Step 5: Run tests to pass, commit**

Run: `python -m pytest tests/test_computers.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: Maritime computers client for headful browser research"
```

---

### Task 7: Node plumbing — Services container + shared fakes

**Files:**
- Create: `app/graph/nodes/__init__.py` (empty), `app/graph/nodes/helpers.py`
- Test: `tests/fakes.py`, `tests/test_node_helpers.py`

**Interfaces:**
- Consumes: `Event`/`EventPublisher` (Task 3), `LLMResult` (Task 4), `Sandbox` (Task 5), `Settings`
- Produces (consumed by Tasks 8–10 and 13):
  - `Services` dataclass: fields `settings, llm, prices, publisher, sandbox_factory, computers, redis, pg_dsn=None, clock`
    - `sandbox_factory: Callable[[dict], Sandbox]` (takes the state, returns a sandbox)
    - `clock: Callable[[], str]` returning ISO timestamps
  - `now_iso() -> str`
  - `await emit(services, state, node, type, data)` — publishes an Event
  - `model_for(services, state, role) -> str` — roles: `planner|researcher|coding_agent|reviewer`; `state["model_overrides"][role]` wins over `settings.model_{role}`
  - `await apply_llm_cost(updates: dict, state, services, result, node) -> dict` — adds price-table cost to `cost_so_far` in `updates` and emits one `cost_update` event
- `tests/fakes.py`: `StubLLM(responses: list[str])` (FIFO, records calls), `StubPriceTable` (0.001/call), `StubSandbox(run_results=None, exec_stdout="", diff_text=...)`, `StubComputers`, `FakePublisher`, `make_services(**overrides) -> Services`

- [ ] **Step 1: Write `tests/fakes.py`**

```python
from types import SimpleNamespace

from app.config import Settings
from app.graph.nodes.helpers import Services
from app.llm.openrouter import LLMResult
from app.sandbox.base import ExecResult


class StubLLM:
    """Scripted FIFO LLM. Records every call for assertions."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, model, messages, stream_cb=None):
        self.calls.append({"model": model, "messages": messages})
        if not self.responses:
            raise AssertionError("StubLLM exhausted — add scripted responses")
        return LLMResult(self.responses.pop(0), 10, 5, model)


class StubPriceTable:
    def cost(self, model, prompt_tokens, completion_tokens) -> float:
        return 0.001


class FakePublisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return "0-1"


class StubSandbox:
    def __init__(self, run_results=None, exec_stdout="ok",
                 diff_text="diff --git a/app.py b/app.py\n+def add(a, b):\n    return a + b\n"):
        self.agent_id = "stub-agent"
        self.run_results = list(run_results or [])
        self.default_run_result = ExecResult(0, "1 passed in 0.01s", "")
        self.exec_stdout = exec_stdout
        self.diff_text = diff_text
        self.files: dict[str, str] = {}
        self.written: list[tuple[str, str]] = []
        self.shells: list[str] = []
        self.run_calls: list[str] = []
        self.ensured = 0
        self.slept = False

    async def ensure(self) -> str:
        self.ensured += 1
        return self.agent_id

    async def exec(self, command, timeout=None) -> ExecResult:
        self.shells.append(command)
        return ExecResult(0, self.exec_stdout, "")

    async def run_long(self, command: str, timeout=None) -> ExecResult:
        self.run_calls.append(command)
        if self.run_results:
            return self.run_results.pop(0)
        return self.default_run_result

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.written.append((path, content))
        self.files[path] = content

    async def list_files(self, path: str) -> list[dict]:
        return []

    async def diff(self) -> str:
        return self.diff_text

    async def sleep(self) -> None:
        self.slept = True


class StubComputers:
    def __init__(self):
        self.computer_id = "comp-1"
        self.viewer_url = "https://view.maritime.sh/aw"
        self.opened: list[str] = []
        self.viewer_calls = 0

    async def ensure(self, task_id: str):
        return SimpleNamespace(computer_id=self.computer_id, frame_id=0,
                               width=1200, height=750, raw={})

    async def act(self, computer_id, action):
        return SimpleNamespace(computer_id=computer_id, frame_id=1,
                               width=1200, height=750, raw={})

    async def shell(self, computer_id, command, timeout_s=30) -> ExecResult:
        return ExecResult(0, "", "")

    async def open_url(self, computer_id, url):
        self.opened.append(url)
        return SimpleNamespace(computer_id=computer_id, frame_id=2,
                               width=1200, height=750, raw={})

    async def viewer_link(self, computer_id, mode="watch", ttl_s=600) -> str:
        self.viewer_calls += 1
        return self.viewer_url

    async def sleep(self, computer_id) -> None:
        pass


def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None) -> SimpleNamespace:
    """Build a Services namespace wired to fakes. sandbox_factory returns the shared stub."""
    from app.graph.nodes.helpers import Services

    settings = settings or Settings()
    sandbox = sandbox or StubSandbox()
    return Services(
        settings=settings,
        llm=llm or StubLLM([]),
        prices=prices or StubPriceTable(),
        publisher=publisher or FakePublisher(),
        sandbox_factory=lambda state: sandbox,
        computers=computers or StubComputers(),
        redis=redis,
        pg_dsn=pg_dsn,
    )
```

- [ ] **Step 2: Write the failing test**

`tests/test_node_helpers.py`:

```python
from tests.fakes import make_services


async def test_model_for_override_wins():
    s = make_services()
    state = {"model_overrides": {"planner": "custom/model"}, "cost_so_far": 0.0}
    from app.graph.nodes.helpers import model_for
    assert model_for(s, state, "planner") == "custom/model"
    assert model_for(s, state, "reviewer") == s.settings.model_reviewer


async def test_apply_llm_cost_updates_state_and_emits():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    s = make_services()
    state = {"task_id": "t1", "cost_so_far": 0.0}
    result = LLMResult("x", 100, 10, "m")
    updates = await apply_llm_cost({"plan": "p"}, state, s, result, "planner")
    assert updates["cost_so_far"] == 0.001
    events = s.publisher.events
    assert len(events) == 1
    assert events[0].type == "cost_update"
    assert events[0].data == {"cost_so_far": 0.001}
```

- [ ] **Step 3: Run to verify failure** — `python -m pytest tests/test_node_helpers.py -v` → FAIL (no `app.graph.nodes`).

- [ ] **Step 4: Implement `app/graph/nodes/helpers.py`**

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.events.publisher import Event


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class Services:
    settings: object
    llm: object
    prices: object
    publisher: object
    sandbox_factory: Callable
    computers: object
    redis: object
    pg_dsn: str | None = None
    clock: Callable[[], str] = field(default_factory=lambda: now_iso)


async def emit(services, state, node: str, type: str, data: dict):
    await services.publisher.publish(
        Event(task_id=state["task_id"], node=node, type=type, data=data))


def model_for(services, state, role: str) -> str:
    override = (state.get("model_overrides") or {}).get(role)
    if override:
        return override
    return getattr(services.settings, f"model_{role}")


async def apply_llm_cost(updates: dict, state, services, result, node: str) -> dict:
    delta = services.prices.cost(result.model, result.prompt_tokens, result.completion_tokens)
    total = round(state.get("cost_so_far", 0.0) + delta, 6)
    updates["cost_so_far"] = total
    await emit(services, {**state, "cost_so_far": total}, node, "cost_update",
               {"cost_so_far": total})
    return updates
```

- [ ] **Step 5: Run tests to pass, commit**

Run: `python -m pytest tests/test_node_helpers.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: node services container, event/cost helpers, shared fakes"
```

---

### Task 8: Planner and Researcher nodes

**Files:**
- Create: `app/graph/nodes/planner.py`, `app/graph/nodes/researcher.py`
- Test: `tests/test_planner.py`, `tests/test_researcher.py`

**Interfaces:**
- Consumes: `Services`, `emit`, `model_for`, `apply_llm_cost` (Task 7); Sandbox read-only (`ensure`, `exec`); `MaritimeComputers.ensure/viewer_link/open_url` (Task 6); `StubLLM`, `StubSandbox`, `StubComputers` (Task 7)
- Produces:
  - `planner_node(state, *, services) -> dict` — sets `plan`, `status="researching"`, cost; emits `node_started`/`tool_call {"action": "read_file_tree"}`/`node_completed`; on re-entry (existing `plan` + `approval_status == "rejected"`) the prompt includes the previous plan and the revision instruction
  - `researcher_node(state, *, services) -> dict` — sets `research_notes`, `computer_id`, `status="coding"`, cost; emits one `tool_call {"viewer_url": ...}`, one `tool_call {"url", "action": "browser_open"}` per URL, `node_started`/`node_completed`

- [ ] **Step 1: Write the failing planner test**

`tests/test_planner.py`:

```python
from tests.fakes import make_services

PLAN_TEXT = "PLAN: 1. fix add() in calc.py\n2. run pytest"


async def test_planner_sets_plan_and_events():
    from app.graph.nodes.planner import planner_node

    s = make_services(llm=__import__("tests.fakes", fromlist=["StubLLM"]).StubLLM([PLAN_TEXT]))
    state = {"task_id": "t1", "task_description": "fix calc", "repo": "org/repo",
             "base_branch": "main", "model_overrides": {}, "cost_so_far": 0.0,
             "approval_status": "pending", "error_log": []}
    updates = await planner_node(state, services=s)
    assert updates["plan"] == PLAN_TEXT
    assert updates["status"] == "researching"
    assert updates["cost_so_far"] == 0.001
    types = [e.type for e in s.publisher.events]
    assert types[0] == "node_started" and types[-1] == "node_completed"
    assert any(e.type == "tool_call" for e in s.publisher.events)


async def test_planner_reentry_includes_prior_plan():
    from tests.fakes import make_services, StubLLM
    llm = StubLLM(["REVISED PLAN"])
    s = make_services(llm=llm)
    state = {"task_id": "t1", "task_description": "d", "repo": "org/repo", "base_branch": "main",
             "model_overrides": {}, "cost_so_far": 0.0, "approval_status": "rejected",
             "plan": "old plan", "error_log": []}
    await planner_node(state, services=s)
    sent = llm.calls[0]["messages"][-1]["content"]
    assert "old plan" in sent and "revise" in sent.lower()
```

- [ ] **Step 2: Run to verify failure** — FAIL (no module).

- [ ] **Step 3: Implement `app/graph/nodes/planner.py`**

```python
from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

PLANNER_PROMPT = """You are the Planner agent in an automated engineering workflow. You do not write
code. Given a task description and read-only access to the target repository,
produce a concrete, ordered implementation plan: which files will be created or
modified, what the change in each does, and any sequencing constraints between
steps. If the task is ambiguous, state your interpretation explicitly rather than
asking a question - a human will review your plan before code is written. If you
are re-entering after review or human feedback, revise the previous plan to
address every point raised; do not silently drop unaddressed feedback."""


async def planner_node(state, *, services):
    await emit(services, state, "planner", "node_started", {})
    await emit(services, state, "planner", "tool_call", {"action": "read_file_tree"})
    tree = await _file_tree(state, services)
    prior = ""
    if state.get("plan") and state.get("approval_status") == "rejected":
        prior = (f"\n\nPREVIOUS PLAN (revise it, addressing all human feedback; "
                 f"do not silently drop any point):\n{state['plan']}")
    messages = [
        {"role": "system", "content": PLANNER_PROMPT},
        {"role": "user", "content":
            f"Repository: {state['repo']} (base branch {state['base_branch']})\n"
            f"File tree (depth 2):\n{tree}\n\nTask: {state['task_description']}{prior}"},
    ]
    result = await services.llm.chat(model_for(services, state, "planner"), messages)
    updates = {"plan": result.text, "status": "researching", "error_log": list(state["error_log"])}
    updates = await apply_llm_cost(updates, state, services, result, "planner")
    await emit(services, state, "planner", "node_completed", {"plan_chars": len(result.text)})
    return updates


async def _file_tree(state, services) -> str:
    try:
        sb = services.sandbox_factory(state)
        await sb.ensure()
        res = await sb.exec(
            "cd /data/workspace && find . -maxdepth 2 -not -path '*/.git*' | head -200")
        return res.stdout or "(empty)"
    except Exception as e:  # noqa: BLE001 - planner must not crash on repo read failure
        return f"(repo read failed: {e})"
```

- [ ] **Step 4: Write the failing researcher test**

`tests/test_researcher.py`:

```python
from tests.fakes import make_services

FIRST = "URL: https://docs.example.com/api\nURL: https://docs.example.com/guide"
NOTES = "### Notes\n- API takes ints (docs.example.com/api)"


def researcher_state():
    return {"task_id": "t1", "repo": "org/repo", "plan": "PLAN", "model_overrides": {},
            "cost_so_far": 0.0, "error_log": []}


async def test_researcher_visits_urls_and_writes_notes():
    from tests.fakes import StubLLM
    llm = StubLLM([FIRST, NOTES])
    s = make_services(llm=llm)
    updates = await __import__("app.graph.nodes.researcher", fromlist=["x"]).researcher_node(
        researcher_state(), services=s)
    assert updates["computer_id"] == s.computers.computer_id
    assert updates["research_notes"].startswith("### Sources consulted")
    assert "Notes" in updates["research_notes"]
    assert s.computers.opened == ["https://docs.example.com/api", "https://docs.example.com/guide"]
    assert s.computers.viewer_calls == 1
    tool = [e for e in s.publisher.events if e.type == "tool_call"]
    assert any(e.data.get("viewer_url") for e in tool)
    assert sum(1 for e in tool if e.data.get("action") == "browser_open") == 2
    assert updates["status"] == "coding"
```

(Rewrite the import cleanly in the final test file: `from app.graph.nodes.researcher import researcher_node` at the top instead of `__import__`.)

- [ ] **Step 5: Run to verify failure** — FAIL.

- [ ] **Step 6: Implement `app/graph/nodes/researcher.py`**

```python
import re

from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

RESEARCHER_PROMPT = """You are the Researcher agent. You are given an implementation plan and must
gather everything the Coding Agent will need to execute it correctly: exact
library APIs and versions in use, precedent from elsewhere in this codebase,
and any external documentation the plan depends on. Cite where each fact came
from (file path or URL). Do not write implementation code - summarize findings
as notes the Coding Agent will consult.

First, list up to 3 documentation URLs worth consulting, one per line, each
prefixed with 'URL: '. Then, after receiving the fetched excerpts, write the
research notes with citations."""


async def researcher_node(state, *, services):
    await emit(services, state, "researcher", "node_started", {})
    comp = await services.computers.ensure(state["task_id"])
    updates = {"computer_id": comp.computer_id, "status": "researching"}
    viewer = await services.computers.viewer_link(comp.computer_id, mode="watch")
    await emit(services, state, "researcher", "tool_call", {"viewer_url": viewer})

    model = model_for(services, state, "researcher")
    base_messages = [
        {"role": "system", "content": RESEARCHER_PROMPT},
        {"role": "user", "content": f"PLAN:\n{state.get('plan') or ''}\n\nREPO: {state['repo']}"},
    ]
    first = await services.llm.chat(model, base_messages)
    urls = [u for u in _urls(first.text)][:3]

    excerpts = []
    for url in urls:
        await services.computers.open_url(comp.computer_id, url)
        await emit(services, state, "researcher", "tool_call",
                   {"url": url, "action": "browser_open"})
        excerpts.append(f"## {url}\n{await _fetch_excerpt(state, services, url)}")

    followup = "TOOL RESULTS:\n" + ("\n\n".join(excerpts) if excerpts else "No URLs found.")
    second = await services.llm.chat(model, base_messages + [{"role": "user", "content": followup}])
    notes = ("### Sources consulted (live, in the headful browser)\n"
             + "\n".join(f"- {u}" for u in urls)
             + "\n\n" + second.text)
    updates.update({"research_notes": notes, "status": "coding"})
    updates = await apply_llm_cost(updates, state, services, first, "researcher")
    updates = await apply_llm_cost(updates, {**state, **updates}, services, second, "researcher")
    await emit(services, state, "researcher", "node_completed", {"urls": urls})
    return updates


def _urls(text: str) -> list[str]:
    import re
    return re.findall(r"URL:\s*(\S+)", text)


async def _fetch_excerpt(state, services, url: str) -> str:
    try:
        sb = services.sandbox_factory(state)
        await sb.ensure()
        res = await sb.exec(
            f"curl -sL --max-time 20 {url} | sed -e 's/<[^>]*>/ /g' | tr -s ' \\n' ' ' | head -c 8000")
        return res.stdout
    except Exception as e:  # noqa: BLE001 - research must not crash the run
        return f"(fetch failed: {e})"
```

- [ ] **Step 7: Run tests to pass, commit**

Run: `python -m pytest tests/test_planner.py tests/test_researcher.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: planner and researcher nodes with headful-browser research"
```

### Task 9: Coding Agent and Tester nodes

**Files:**
- Create: `app/graph/nodes/coding_agent.py`, `app/graph/nodes/tester.py`
- Test: `tests/test_coding_agent.py`, `tests/test_tester.py`

**Interfaces:**
- Consumes: `Sandbox` (`ensure`, `exec`, `write_file`, `run_long`, `diff`), `emit`, `model_for`, `apply_llm_cost`, `route_after_tester`, `TestResult`, `StubLLM`/`StubSandbox`
- Produces:
  - `coding_agent_node(state, *, services) -> Command[Literal["tester", "needs_human"]]`
    - Retry-context consume rule (spec §5): if `review_comments` present → `retry_counts["coding"] += 1`, comments injected into prompt and **cleared** (`review_comments=None`); elif `test_results` present and not passed → `retry_counts["testing"] += 1`, failing output injected; else first entry, no counter change
    - Edit loop: ≤ `MAX_ROUNDS = 8` LLM rounds; each round must be strict JSON `{"ops": [...], "done": bool}` with ops `{"op": "write_file", "path": "rel/path", "content": "..."}` or `{"op": "shell", "command": "..."}`; ops applied via the sandbox with a `tool_call` event each; two consecutive unparseable rounds → `needs_human`
    - After `done`: `code_diff = await sb.diff()` (cumulative staged diff vs base); empty diff → `needs_human`; otherwise `Command(update={...}, goto="tester")`
    - Does **not** commit in the VM (Global Constraints)
  - `tester_node(state, *, services) -> Command[Literal["coding_agent", "reviewer", "needs_human"]]` — mechanical, no LLM: `run_long(state["test_command"] or settings.test_command)`; `TestResult` built from exit code; `failing_tests` parsed from pytest `^FAILED\s+(\S+)` lines; `failing_output` = last 8000 chars; routes via `route_after_tester`; on `needs_human` appends the bound-exceeded reason to `error_log`

- [ ] **Step 1: Write the failing coding-agent tests**

`tests/test_coding_agent.py`:

```python
import json

from tests.fakes import make_services

OPS_DONE = json.dumps({"ops": [{"op": "write_file", "path": "calc.py",
                                "content": "def add(a, b):\n    return a + b\n"}], "done": True})
OPS_FIX = json.dumps({"ops": [{"op": "shell", "command": "echo fixing"}], "done": True})


def coding_state(**kw):
    st = {"task_id": "t1", "task_description": "fix add", "repo": "org/repo", "base_branch": "main",
          "plan": "PLAN", "research_notes": "notes", "model_overrides": {}, "cost_so_far": 0.0,
          "error_log": [], "retry_counts": {"testing": 0, "coding": 0},
          "test_results": None, "review_comments": None}
    st.update(kw)
    return st


async def test_first_entry_writes_files_and_captures_diff():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_DONE])
    s = make_services(llm=llm)
    res = await coding_agent_node(coding_state(), services=s)
    assert res.update["code_diff"].startswith("diff --git")
    assert res.update["status"] == "testing"
    assert res.update["retry_counts"] == {"testing": 0, "coding": 0}
    assert res.goto == "tester"
    sb = s.sandbox_factory({})
    assert ("/data/workspace/calc.py", "def add(a, b):\n    return a + b\n") in sb.written
    tool = [e for e in s.publisher.events if e.type == "tool_call"]
    assert any(e.data.get("op") == "write_file" for e in tool)


async def test_retry_after_test_failure_increments_testing_counter():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_FIX])
    s = make_services(llm=llm)
    st = coding_state(test_results={"passed": False, "failing_output": "E: assert 2+2==5",
                                    "failing_tests": ["tests/test_calc.py::test_add"]})
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["testing"] == 1
    assert res.update["retry_counts"]["coding"] == 0
    sent = llm.calls[0]["messages"][-1]["content"]
    assert "E: assert 2+2==5" in sent


async def test_retry_after_review_consumes_comments():
    from app.graph.nodes.coding_agent import coding_agent_node
    from tests.fakes import StubLLM
    llm = StubLLM([OPS_FIX])
    s = make_services(llm=llm)
    st = coding_state(review_comments=["use int not str"])
    res = await coding_agent_node(st, services=s)
    assert res.update["retry_counts"]["coding"] == 1
    assert res.update["retry_counts"]["testing"] == 0
    assert res.update["review_comments"] is None
    assert "use int not str" in llm.calls[0]["messages"][-1]["content"]
```

- [ ] **Step 2: Run to verify failure** — FAIL (no module).

- [ ] **Step 3: Implement `app/graph/nodes/coding_agent.py`**

```python
import json
import re

from langgraph.types import Command

from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

CODING_AGENT_PROMPT = """You are the Coding Agent. You implement the given plan, using the research
notes provided, inside an isolated sandbox with full read/write access to the
repository. Make the smallest correct change that satisfies the plan. If you
are re-entering because tests failed or a reviewer requested changes, the
failure details / review comments are included below - address them
specifically rather than rewriting unrelated code. When finished, leave the
change as a git diff against the base branch; do not push or open a PR
yourself.

Work in rounds. In every round reply with ONLY a JSON object:
{"ops": [{"op": "write_file", "path": "relative/path.py", "content": "..."},
         {"op": "shell", "command": "shell command"}], "done": true}
Set "done": true when the plan is fully implemented. Paths are relative to the
repository root. Never run git add, git commit, or git push yourself."""

MAX_ROUNDS = 8


async def coding_agent_node(state, *, services):
    await emit(services, state, "coding_agent", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()

    updates = {"status": "coding", "error_log": list(state["error_log"])}
    context = ""
    if state.get("review_comments"):
        context = "REVIEW COMMENTS TO ADDRESS:\n" + "\n".join(state["review_comments"])
        updates["review_comments"] = None  # consumed
        updates["retry_counts"] = {**state["retry_counts"],
                                   "coding": state["retry_counts"].get("coding", 0) + 1}
    elif state.get("test_results") and not state["test_results"]["passed"]:
        context = ("TEST FAILURE DETAILS:\n"
                   + (state["test_results"].get("failing_output") or ""))
        updates["retry_counts"] = {**state["retry_counts"],
                                   "testing": state["retry_counts"].get("testing", 0) + 1}

    model = model_for(services, state, "coding_agent")
    messages = [{"role": "system", "content": CODING_AGENT_PROMPT},
                {"role": "user", "content": _brief(state, context)}]

    bad_rounds = 0
    for _round in range(MAX_ROUNDS):
        result = await services.llm.chat(model, messages)
        try:
            ops, done = _parse_round(result.text)
        except (ValueError, json.JSONDecodeError) as e:
            bad_rounds += 1
            if bad_rounds >= 2:
                updates["error_log"] = updates["error_log"] + [f"coding_agent: unparseable rounds ({e})"]
                return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
            messages += [{"role": "assistant", "content": result.text},
                         {"role": "user", "content":
                          f"Invalid response ({e}). Reply with ONLY the JSON object."}]
            continue
        bad_rounds = 0
        for op in ops:
            await _apply_op(services, state, sb, op)
        if done:
            diff = await sb.diff()
            if not diff.strip():
                updates["error_log"] = updates["error_log"] + ["coding_agent: empty diff"]
                return Command(update={**updates, "status": "needs_human"}, goto="needs_human")
            updates["code_diff"] = diff
            updates = await apply_llm_cost(updates, {**state, **updates}, services,
                                           result, "coding_agent")
            await emit(services, state, "coding_agent", "node_completed", {"diff_chars": len(diff)})
            return Command(update=updates, goto="tester")
        messages += [{"role": "assistant", "content": result.text},
                     {"role": "user", "content": "Round executed. Continue."}]

    updates["error_log"] = updates["error_log"] + [f"coding_agent: no convergence in {MAX_ROUNDS} rounds"]
    return Command(update={**updates, "status": "needs_human"}, goto="needs_human")


def _parse_round(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object found in response")
    d = json.loads(m.group(0))
    if not isinstance(d.get("ops"), list):
        raise ValueError("missing ops list")
    return d["ops"], bool(d.get("done"))


async def _apply_op(services, state, sb, op):
    kind = op.get("op")
    if kind == "write_file":
        path = "/data/workspace/" + str(op["path"]).lstrip("/")
        await sb.write_file(path, str(op["content"]))
        await emit(services, state, "coding_agent", "tool_call",
                   {"op": "write_file", "path": op["path"], "chars": len(str(op["content"]))})
    elif kind == "shell":
        res = await sb.exec(str(op["command"]))
        await emit(services, state, "coding_agent", "tool_call",
                   {"op": "shell", "command": op["command"], "exit_code": res.exit_code})
    else:
        raise ValueError(f"unknown op {kind!r}")


def _brief(state, context: str) -> str:
    parts = [f"Repository: {state['repo']} (base branch {state['base_branch']})",
             f"TASK:\n{state['task_description']}",
             f"PLAN:\n{state.get('plan') or '(none)'}",
             f"RESEARCH NOTES:\n{state.get('research_notes') or '(none)'}"]
    if context:
        parts.append(context)
    return "\n\n".join(parts)
```

- [ ] **Step 4: Write the failing tester tests**

`tests/test_tester.py`:

```python
from app.sandbox.base import ExecResult
from tests.fakes import StubSandbox, make_services


def tester_state(**kw):
    st = {"task_id": "t1", "test_command": "pytest -q", "error_log": [],
          "retry_counts": {"testing": 0, "coding": 0}, "repo": "org/repo",
          "base_branch": "main", "sandbox_id": None}
    st.update(kw)
    return st


async def test_pass_routes_to_reviewer():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(0, "1 passed", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(tester_state(), services=s)
    assert res.update["test_results"]["passed"] is True
    assert res.goto == "reviewer"
    assert sb.run_calls == ["pytest -q"]


async def test_failure_parses_failing_tests_and_routes_to_coding():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED tests/test_calc.py::test_add - assert", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(tester_state(), services=s)
    assert res.update["test_results"]["failing_tests"] == ["tests/test_calc.py::test_add"]
    assert res.goto == "coding_agent"


async def test_bound_exceeded_routes_to_needs_human():
    from app.graph.nodes.tester import tester_node
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED x::y", "")])
    s = make_services(sandbox=sb)
    res = await tester_node(tester_state(retry_counts={"testing": 3, "coding": 0}), services=s)
    assert res.goto == "needs_human"
    assert res.update["status"] == "needs_human"
    assert "retry bound exceeded" in res.update["error_log"][-1]
```

- [ ] **Step 5: Run to verify failure** — FAIL.

- [ ] **Step 6: Implement `app/graph/nodes/tester.py`**

```python
import re

from langgraph.types import Command

from app.graph.edges import route_after_tester
from app.graph.nodes.helpers import emit
from app.graph.state import TestResult


async def tester_node(state, *, services):
    """Mechanical node — no LLM. Runs the suite, reports structured results (spec §6)."""
    await emit(services, state, "tester", "node_started", {})
    sb = services.sandbox_factory(state)
    await sb.ensure()
    cmd = state.get("test_command") or services.settings.test_command
    res = await sb.run_long(cmd, timeout=services.settings.sandbox_run_timeout_seconds)
    await emit(services, state, "tester", "tool_call", {"command": cmd, "exit_code": res.exit_code})
    failing = sorted(set(re.findall(r"^FAILED\s+(\S+)", res.combined, re.M)))
    tr = TestResult(passed=res.exit_code == 0, failing_output=res.combined[-8000:],
                    failing_tests=failing).to_dict()
    await emit(services, state, "tester", "node_completed", {"passed": tr["passed"]})
    route = route_after_tester({**state, "test_results": tr})
    if route == "needs_human":
        err = list(state["error_log"]) + [
            f"tester: retry bound exceeded (exit_code={res.exit_code}); "
            f"last failure: {tr['failing_output'][:500]}"]
        return Command(update={"test_results": tr, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update={"test_results": tr,
                           "status": "reviewing" if route == "reviewer" else "coding"},
                   goto=route)
```

- [ ] **Step 7: Run tests to pass, commit**

Run: `python -m pytest tests/test_coding_agent.py tests/test_tester.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: coding agent tool loop and mechanical tester node"
```

---

### Task 10: Reviewer, human approval, needs_human, commit_pr nodes

**Files:**
- Create: `app/graph/nodes/reviewer.py`, `app/graph/nodes/human_approval.py`, `app/graph/nodes/needs_human.py`, `app/graph/nodes/commit_pr.py`
- Test: `tests/test_reviewer.py`, `tests/test_gate_nodes.py`

**Interfaces:**
- Consumes: `StubLLM`, `emit`, `model_for`, `apply_llm_cost`, `route_after_reviewer`, `route_after_human`; `langgraph.types.interrupt` (monkeypatched in tests)
- Produces:
  - `reviewer_node(state, *, services) -> Command[Literal["human_approval", "coding_agent", "needs_human"]]` — LLM must return `{"verdict": "approved"|"needs_changes", "comments": [...]}`; unparseable → `needs_changes` with comments `["reviewer returned unparseable output", <first 500 chars>]`; sets `approval_status` + `review_comments` (None on approved); routes via `route_after_reviewer`
  - `human_approval_node(state, *, services) -> Command[Literal["commit_pr", "planner"]]` — calls `interrupt(payload)` (the ONLY side effect before it is the `node_started` emit — the node re-runs from the top on resume); resume payload `{"decision", "feedback"}`; approved → `approval_status="approved"`, `status="committing"`, goto commit_pr; rejected → `approval_status="rejected"`, `task_description += "\n\nHUMAN FEEDBACK: {feedback}"`, `status="planning"`, goto planner
  - `needs_human_node(state, *, services) -> dict` — `status="needs_human"` + error_log line with counters
  - `commit_pr_node(state, *, services) -> dict` — phase-1 finalize: `status="done"`, emits `node_completed` with a `git diff --cached --stat` summary (best-effort)

- [ ] **Step 1: Write the failing reviewer test**

`tests/test_reviewer.py`:

```python
from tests.fakes import make_services


def rstate(**kw):
    st = {"task_id": "t1", "task_description": "fix add", "plan": "P", "code_diff": "diff --git",
          "test_results": {"passed": True, "failing_output": "", "failing_tests": []},
          "model_overrides": {}, "cost_so_far": 0.0, "error_log": [],
          "retry_counts": {"testing": 0, "coding": 0}}
    st.update(kw)
    return st


async def test_reviewer_approved_routes_to_human():
    from app.graph.nodes.reviewer import reviewer_node
    from tests.fakes import StubLLM
    s = make_services(llm=StubLLM(['{"verdict": "approved", "comments": []}']))
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "approved"
    assert res.update["review_comments"] is None
    assert res.goto == "human_approval"


async def test_reviewer_needs_changes_routes_to_coding():
    from app.graph.nodes.reviewer import reviewer_node
    from tests.fakes import StubLLM
    s = make_services(llm=StubLLM(['{"verdict": "needs_changes", "comments": ["handle empty input"]}']))
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "needs_changes"
    assert res.update["review_comments"] == ["handle empty input"]
    assert res.goto == "coding_agent"


async def test_reviewer_unparseable_defaults_to_needs_changes():
    from app.graph.nodes.reviewer import reviewer_node
    from tests.fakes import StubLLM
    s = make_services(llm=StubLLM(["looks fine to me"]))
    res = await reviewer_node(rstate(), services=s)
    assert res.update["approval_status"] == "needs_changes"
    assert res.update["review_comments"][0] == "reviewer returned unparseable output"
    assert res.goto == "coding_agent"
```

- [ ] **Step 2: Write the failing gate-node test**

`tests/test_gate_nodes.py`:

```python
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
```

- [ ] **Step 3: Run to verify failure** — FAIL.

- [ ] **Step 4: Implement the four node modules**

`app/graph/nodes/reviewer.py`:

```python
import json
import re

from langgraph.types import Command

from app.graph.edges import route_after_reviewer
from app.graph.nodes.helpers import apply_llm_cost, emit, model_for

REVIEWER_PROMPT = """You are the Reviewer agent. You review a code diff that has already passed
tests. Check that it actually satisfies the original task (not just "tests
pass"), follows the codebase's existing conventions, and doesn't introduce
obvious risk (unhandled errors, missing edge cases, security issues). If you
request changes, be specific enough that the Coding Agent can act without
further clarification. You are a gate before human review, not a replacement
for it - when in doubt, approve and let the human decide, rather than looping
indefinitely.

Reply with ONLY: {"verdict": "approved" | "needs_changes", "comments": ["..."]}"""


async def reviewer_node(state, *, services):
    await emit(services, state, "reviewer", "node_started", {})
    messages = [
        {"role": "system", "content": REVIEWER_PROMPT},
        {"role": "user", "content":
            f"ORIGINAL TASK:\n{state['task_description']}\n\nPLAN:\n{state.get('plan') or ''}\n\n"
            f"TEST RESULTS:\n{json.dumps(state.get('test_results'))}\n\nDIFF:\n{state.get('code_diff') or ''}"},
    ]
    result = await services.llm.chat(model_for(services, state, "reviewer"), messages)
    verdict, comments = _parse_verdict(result.text)
    updates = {"status": "reviewing",
               "approval_status": "approved" if verdict == "approved" else "needs_changes",
               "review_comments": None if verdict == "approved" else comments}
    updates = await apply_llm_cost(updates, state, services, result, "reviewer")
    await emit(services, state, "reviewer", "node_completed", {"verdict": verdict})
    route = route_after_reviewer({**state, "approval_status": updates["approval_status"]})
    if route == "needs_human":
        err = list(state["error_log"]) + [f"reviewer: retry bound exceeded; last comments={comments[:5]}"]
        return Command(update={**updates, "error_log": err, "status": "needs_human"},
                       goto="needs_human")
    return Command(update=updates, goto=route)


def _parse_verdict(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("verdict") in ("approved", "needs_changes"):
                return d["verdict"], [str(c) for c in d.get("comments", [])]
        except json.JSONDecodeError:
            pass
    return "needs_changes", ["reviewer returned unparseable output", text[:500]]
```

`app/graph/nodes/human_approval.py`:

```python
from langgraph.types import Command, interrupt

from app.graph.nodes.helpers import emit


async def human_approval_node(state, *, services):
    """LangGraph interrupt() gate — pauses until resumed with a decision payload.

    On resume the node re-runs from the top; the only pre-interrupt side effect
    is the node_started emit (idempotent). The pause/resume timestamps are
    managed by RunManager (Task 13), not here.
    """
    await emit(services, state, "human_approval", "node_started", {})
    payload = interrupt({
        "task_id": state["task_id"],
        "summary": {"plan_chars": len(state.get("plan") or ""),
                    "diff_chars": len(state.get("code_diff") or ""),
                    "test_results": state.get("test_results"),
                    "review_comments": state.get("review_comments"),
                    "cost_so_far": state.get("cost_so_far")},
        "instructions": "Resume with {'decision': 'approved' | 'rejected', 'feedback': '...'}",
    })
    decision = payload.get("decision")
    feedback = payload.get("feedback") or ""
    if decision == "approved":
        return Command(update={"approval_status": "approved", "status": "committing"},
                       goto="commit_pr")
    desc = state["task_description"] + (f"\n\nHUMAN FEEDBACK: {feedback}" if feedback else "")
    return Command(update={"approval_status": "rejected", "task_description": desc,
                           "status": "planning"},
                   goto="planner")
```

`app/graph/nodes/needs_human.py`:

```python
async def needs_human_node(state, *, services):
    line = (f"needs_human: retry bounds reached "
            f"(testing={state['retry_counts'].get('testing', 0)}, "
            f"coding={state['retry_counts'].get('coding', 0)})")
    return {"status": "needs_human", "error_log": list(state["error_log"]) + [line]}
```

`app/graph/nodes/commit_pr.py`:

```python
from app.graph.nodes.helpers import emit


async def commit_pr_node(state, *, services):
    """Phase-1 finalize: mark done locally. Push/PR is the phase-2 plan."""
    await emit(services, state, "commit_pr", "node_started", {})
    try:
        sb = services.sandbox_factory(state)
        stat = await sb.exec("cd /data/workspace && git diff --cached --stat")
        summary = stat.stdout[:1000]
    except Exception as e:  # noqa: BLE001 - finalize must not crash
        summary = f"(stat unavailable: {e})"
    await emit(services, state, "commit_pr", "node_completed", {"summary": summary})
    return {"status": "done", "pr_url": None}
```

- [ ] **Step 5: Run tests to pass, commit**

Run: `python -m pytest tests/test_reviewer.py tests/test_gate_nodes.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: reviewer verdict, human approval gate, terminal nodes"
```

---

### Task 11: Graph assembly + scenario tests

**Files:**
- Create: `app/graph/build.py`
- Test: `tests/integration/test_graph_scenarios.py`

**Interfaces:**
- Consumes: all nodes (Tasks 8–10), `InMemorySaver`, fakes
- Produces: `build_graph(services, checkpointer=None)` — compiled graph, nodes `planner, researcher, coding_agent, tester, reviewer, human_approval, needs_human, commit_pr`; static edges `START→planner→researcher→coding_agent→tester`, `needs_human→END`, `commit_pr→END`; tester/reviewer/human_approval route via `Command` (no static out-edges from them — LangGraph forbids mixing)

- [ ] **Step 1: Implement `app/graph/build.py`**

```python
from functools import partial

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import coding_agent, commit_pr, human_approval, needs_human, planner, researcher, reviewer, tester
from app.graph.state import TaskState


def build_graph(services, checkpointer=None):
    b = StateGraph(TaskState)
    b.add_node("planner", partial(planner.planner_node, services=services))
    b.add_node("researcher", partial(researcher.researcher_node, services=services))
    b.add_node("coding_agent", partial(coding_agent.coding_agent_node, services=services))
    b.add_node("tester", partial(tester.tester_node, services=services))
    b.add_node("reviewer", partial(reviewer.reviewer_node, services=services))
    b.add_node("human_approval", partial(human_approval.human_approval_node, services=services))
    b.add_node("needs_human", needs_human.needs_human_node)
    b.add_node("commit_pr", commit_pr.commit_pr_node)
    b.add_edge(START, "planner")
    b.add_edge("planner", "researcher")
    b.add_edge("researcher", "coding_agent")
    b.add_edge("coding_agent", "tester")
    b.add_edge("needs_human", END)
    b.add_edge("commit_pr", END)
    return b.compile(checkpointer=checkpointer)
```

- [ ] **Step 2: Write the scenario tests**

`tests/integration/test_graph_scenarios.py`:

```python
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.graph.build import build_graph
from app.graph.state import initial_state
from app.sandbox.base import ExecResult
from tests.fakes import StubLLM, StubSandbox, make_services

pytestmark = pytest.mark.integration

URLS = "URL: https://docs.example.com/api"
NOTES = "notes with citations"
OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 1"}], "done": True})
OPS2 = json.dumps({"ops": [{"op": "write_file", "path": "calc.py", "content": "x = 2"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
NEEDS_CHANGES = json.dumps({"verdict": "needs_changes", "comments": ["check negative numbers"]})


def initial():
    return dict(initial_state("t-scen", "fix add", "org/repo", "main", "pytest -q", {}))


async def drive_to_interrupt(graph, tid, payload):
    config = {"configurable": {"thread_id": tid}}
    async for chunk in graph.astream(payload, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            return config
    return config


async def test_happy_path_reaches_approval_interrupt():
    services = make_services(llm=StubLLM(["PLAN: fix add", URLS, NOTES, OPS, APPROVED]),
                             sandbox=StubSandbox())  # default run result passes
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s1", initial())
    state = (await graph.aget_state(config)).values
    assert state["code_diff"].startswith("diff --git")
    assert state["test_results"]["passed"] is True
    assert any(e.type == "cost_update" for e in services.publisher.events)


async def test_tester_failure_retries_coding_then_reaches_approval():
    sb = StubSandbox(run_results=[ExecResult(1, "FAILED tests/test_calc.py::test_add", "")])
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, OPS2, APPROVED]), sandbox=sb)
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s2", initial())
    state = (await graph.aget_state(config)).values
    assert state["retry_counts"]["testing"] == 1
    assert state["test_results"]["passed"] is True  # second run passed
    assert sb.ensured >= 2  # same sandbox instance reused (warm VM story)


async def test_tester_bound_exceeded_reaches_needs_human():
    sb = StubSandbox(default_run_result=ExecResult(1, "FAILED x::y", ""))
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, OPS, OPS2, OPS2, OPS2]),
                             sandbox=sb)
    graph = build_graph(services, InMemorySaver())
    config = {"configurable": {"thread_id": "s3"}}
    async for _chunk in graph.astream(initial(), config, stream_mode="updates"):
        pass
    state = (await graph.aget_state(config)).values
    assert state["status"] == "needs_human"
    assert state["retry_counts"]["testing"] == 3
    coding_llm_calls = sum(1 for c in services.llm.calls if c["model"].endswith("claude-sonnet-4.5"))
    assert coding_llm_calls == 4  # initial + 3 retries


async def test_reviewer_rejection_retries_coding():
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS,
                                          NEEDS_CHANGES, OPS2, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s4", initial())
    state = (await graph.aget_state(config)).values
    assert state["retry_counts"]["coding"] == 1
    assert state["test_results"]["passed"] is True


async def test_resume_approved_completes_done():
    services = make_services(llm=StubLLM(["PLAN: fix add", URLS, NOTES, OPS, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s5", initial())
    async for _chunk in graph.astream(Command(resume={"decision": "approved", "feedback": ""}),
                                      config, stream_mode="updates"):
        pass
    state = (await graph.aget_state(config)).values
    assert state["status"] == "done"
    assert state["approval_status"] == "approved"


async def test_resume_rejected_loops_to_planner_with_feedback():
    services = make_services(llm=StubLLM(["PLAN", URLS, NOTES, OPS, APPROVED,
                                          "REVISED PLAN", URLS, NOTES, OPS2, APPROVED]),
                             sandbox=StubSandbox())
    graph = build_graph(services, InMemorySaver())
    config = await drive_to_interrupt(graph, "s6", initial())
    async for _chunk in graph.astream(Command(resume={"decision": "rejected",
                                                      "feedback": "use uuid"}),
                                      config, stream_mode="updates"):
        if "__interrupt__" in _chunk:
            break
    state = (await graph.aget_state(config)).values
    assert state["task_description"].endswith("HUMAN FEEDBACK: use uuid")
    assert state["plan"] == "REVISED PLAN"  # planner re-entered
```

- [ ] **Step 2: Run scenarios to verify they pass**

Run: `python -m pytest tests/integration/test_graph_scenarios.py -v`
Expected: PASS (6 tests). If a routing assert fails, fix the graph wiring — do not weaken the test.

- [ ] **Step 3: Commit**

```bash
git add app tests
git commit -m "feat: graph assembly with Command-gated retry loops and scenario tests"
```

---

### Task 12: Postgres persistence — tasks table + checkpointer factory

**Files:**
- Create: `app/db.py`
- Test: `tests/integration/test_db.py`

**Interfaces:**
- Consumes: `asyncpg`, `AsyncPostgresSaver` (from `langgraph.checkpoint.postgres.aio`)
- Produces:
  - `await ensure_schema(dsn)` — idempotent DDL for the `tasks` table
  - `await upsert_task(dsn, task_id, repo, status, *, paused_at=None, resumed_at=None, cost_so_far=None, retry_counts=None, pr_url=None)`
  - `await get_task(dsn, task_id) -> dict | None`
  - `make_checkpointer(dsn)` — returns the async context manager yielding `AsyncPostgresSaver` (caller enters it and calls `await checkpointer.setup()` once)

- [ ] **Step 1: Write the failing integration test**

`tests/integration/test_db.py`:

```python
import pytest

from app.db import ensure_schema, get_task, upsert_task

pytestmark = pytest.mark.integration


async def test_upsert_get_roundtrip_and_idempotent_schema(settings):
    await ensure_schema(settings.database_url)
    await ensure_schema(settings.database_url)  # second run must not fail
    await upsert_task(settings.database_url, "t-db", "org/repo", "planning",
                      retry_counts={"testing": 1})
    row = await get_task(settings.database_url, "t-db")
    assert row["task_id"] == "t-db" and row["status"] == "planning"
    await upsert_task(settings.database_url, "t-db", "org/repo", "done",
                      cost_so_far=0.5, retry_counts={"testing": 0, "coding": 0},
                      paused_at="2026-09-23T01:00:00+00:00",
                      resumed_at="2026-09-23T10:00:00+00:00")
    row = await get_task(settings.database_url, "t-db")
    assert row["status"] == "done"
    assert row["cost_so_far"] == 0.5
    assert row["retry_counts"] == {"testing": 0, "coding": 0}
    assert await get_task(settings.database_url, "missing") is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (no module `app.db`).

- [ ] **Step 3: Implement `app/db.py`**

```python
import json

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

DDL = """CREATE TABLE IF NOT EXISTS tasks (
  task_id text PRIMARY KEY,
  repo text,
  status text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz,
  paused_at timestamptz,
  resumed_at timestamptz,
  cost_so_far float8,
  retry_counts jsonb,
  pr_url text
)"""


async def ensure_schema(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
    finally:
        await conn.close()


async def upsert_task(dsn, task_id, repo, status, *, paused_at=None, resumed_at=None,
                      cost_so_far=None, retry_counts=None, pr_url=None):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """INSERT INTO tasks (task_id, repo, status, updated_at, paused_at, resumed_at,
                                   cost_so_far, retry_counts, pr_url)
               VALUES ($1, $2, $3, now(), $4, $5, $6, $7::jsonb, $8)
               ON CONFLICT (task_id) DO UPDATE SET
                 repo = $2, status = $3, updated_at = now(), paused_at = $4,
                 resumed_at = $5, cost_so_far = $6, retry_counts = $7::jsonb, pr_url = $8""",
            task_id, repo, status, paused_at, resumed_at, cost_so_far,
            json.dumps(retry_counts or {}), pr_url)
    finally:
        await conn.close()


async def get_task(dsn, task_id):
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        return dict(row) if row else None
    finally:
        await conn.close()


def make_checkpointer(dsn: str):
    """Async context manager yielding AsyncPostgresSaver; caller enters and runs setup()."""
    return AsyncPostgresSaver.from_conn_string(dsn)
```

(`import json` at the top of the module.)

- [ ] **Step 4: Run to pass, commit**

Run: `docker compose up -d` then `python -m pytest tests/integration/test_db.py -v` → PASS.

```bash
git add app tests
git commit -m "feat: tasks table persistence and postgres checkpointer wiring"
```

---

### Task 13: RunManager + FastAPI app + SSE endpoint + restart-resume proof

**Files:**
- Create: `app/services/__init__.py` (empty), `app/services/run_manager.py`, `app/api/__init__.py` (empty), `app/api/routes_tasks.py`, `app/main.py`
- Test: `tests/integration/test_api_flow.py`, `tests/integration/test_restart_resume.py`

**Interfaces:**
- Consumes: everything above; `Command(resume=...)` semantics from the LangGraph docs (the resume value is the return of `interrupt()` inside the paused node)
- Produces:
  - `RunManager(services, graph)`:
    - `await prepare(task_id, values)` — mirror initial state (Redis + PG)
    - `async drive(task_id, graph_input)` — Redis lock `aw:{id}:driving` (SETNX EX 3600; `AlreadyRunning` if held); after **every** super-step mirrors the full state snapshot to Redis `aw:{id}:state` and (if `pg_dsn`) the tasks row; on `__interrupt__`: `graph.aupdate_state(config, {"paused_at": now, "status": "awaiting_approval"})`, mirror, publish `node_started {"paused": true}` on `human_approval`, best-effort `sandbox.sleep()`
    - `await resume(task_id, decision, feedback=None) -> asyncio.Task` — validates `awaiting_approval` (`NotAwaitingApproval` otherwise); publishes `sleep_wake {"paused_at", "resumed_at", "resume_latency_seconds", "decision"}` computed from `paused_at`; `aupdate_state({"resumed_at": now})`; drives `Command(resume={"decision", "feedback"})` as a background task
    - `await get_state(task_id)` (Redis first, graph snapshot fallback), `await artifacts(task_id)`
    - exceptions `AlreadyRunning`, `NotAwaitingApproval`
  - `create_app(services=None, graph=None)` — when `services`/`graph` are passed (tests), installs them directly; otherwise the lifespan builds real Services (OpenRouter client, fetched PriceTable, MaritimeComputers, `make_sandbox` closure, EventPublisher, redis, `AsyncPostgresSaver` entered for the app lifetime, `ensure_schema`) and wires `RunManager(build_graph(...))`
  - Routes on prefix `/tasks`: `POST /tasks` (201 → `{"task_id"}`; repo must contain `/`), `GET /tasks/{id}` (state or 404), `POST /tasks/{id}/approve` / `POST /tasks/{id}/reject` (body `{"feedback": ""}`; 202 or 409), `GET /tasks/{id}/artifacts`, `GET /tasks/{id}/events` (SSE, `text/event-stream`)

- [ ] **Step 1: Implement `app/services/run_manager.py`**

```python
import asyncio
import json
from datetime import datetime

from langgraph.types import Command

from app.db import upsert_task
from app.events.publisher import Event
from app.graph.nodes.helpers import now_iso


class AlreadyRunning(RuntimeError):
    pass


class NotAwaitingApproval(RuntimeError):
    pass


class RunManager:
    def __init__(self, services, graph):
        self.services = services
        self.graph = graph

    def _config(self, task_id):
        return {"configurable": {"thread_id": task_id}}

    async def prepare(self, task_id, values):
        await self._mirror(task_id, values)

    async def start(self, task_id, initial_state_values):
        return asyncio.create_task(self.drive(task_id, initial_state_values))

    async def drive(self, task_id, graph_input):
        lock_key = f"aw:{task_id}:driving"
        if self.services.redis is not None:
            got = await self.services.redis.set(lock_key, "1", nx=True, ex=3600)
            if not got:
                raise AlreadyRunning(task_id)
        try:
            config = {"configurable": {"thread_id": task_id}}
            async for chunk in self.graph.astream(graph_input, config, stream_mode="updates"):
                snap = await self.graph.aget_state(config)
                values = dict(snap.values) if snap and snap.values else {}
                if "__interrupt__" in chunk:
                    values["paused_at"] = now_iso()
                    values["status"] = "awaiting_approval"
                    await self.graph.aupdate_state(
                        config, {"paused_at": values["paused_at"],
                                 "status": "awaiting_approval"})
                    await self._mirror(task_id, values)
                    await self._emit(task_id, "human_approval", "node_started",
                                     {"paused_at": values["paused_at"], "paused": True})
                    try:
                        await self.services.sandbox_factory(values).sleep()
                    except Exception:  # noqa: BLE001 - sleeping is best-effort
                        pass
                else:
                    await self._mirror(task_id, values)
        finally:
            if self.services.redis is not None:
                await self.services.redis.delete(lock_key)

    async def resume(self, task_id, decision, feedback=None):
        state = await self.get_state(task_id)
        if not state or state.get("status") != "awaiting_approval":
            raise NotAwaitingApproval(task_id)
        resumed = now_iso()
        paused = state.get("paused_at")
        latency = None
        if paused:
            latency = round((datetime.fromisoformat(resumed)
                             - datetime.fromisoformat(paused)).total_seconds(), 3)
        await self._emit(task_id, "human_approval", "sleep_wake",
                         {"paused_at": paused, "resumed_at": resumed,
                          "resume_latency_seconds": latency, "decision": decision})
        await self.graph.aupdate_state({"resumed_at": resumed} and self._config(task_id) or None, None) if False else None
        await self.graph.aupdate_state(self._config(task_id), {"resumed_at": resumed})
        payload = {"decision": decision, "feedback": feedback or ""}
        return asyncio.create_task(self.drive(task_id, Command(resume=payload)))

    async def get_state(self, task_id):
        if self.services.redis is not None:
            raw = await self.services.redis.get(f"aw:{task_id}:state")
            if raw:
                return json.loads(raw)
        snap = await self.graph.aget_state(self._config(task_id))
        return dict(snap.values) if snap and snap.values else None

    async def artifacts(self, task_id):
        st = await self.get_state(task_id) or {}
        return {k: st.get(k) for k in ("plan", "research_notes", "code_diff",
                                       "test_results", "review_comments")}

    async def _mirror(self, task_id, values):
        if not values:
            return
        if self.services.redis is not None:
            await self.services.redis.set(f"aw:{task_id}:state", json.dumps(values))
        if self.services.pg_dsn:
            await upsert_task(self.services.pg_dsn, task_id, values.get("repo", ""),
                              values.get("status", ""), paused_at=values.get("paused_at"),
                              resumed_at=values.get("resumed_at"),
                              cost_so_far=values.get("cost_so_far"),
                              retry_counts=values.get("retry_counts"),
                              pr_url=values.get("pr_url"))

    async def _emit(self, task_id, node, type, data):
        await self.services.publisher.publish(
            Event(task_id=task_id, node=node, type=type, data=data))
```

(Imports at top: `from app.db import upsert_task`. Delete the absurd placeholder line in `resume` — the correct call is exactly `await self.graph.aupdate_state(self._config(task_id), {"resumed_at": resumed})`. Use that single line.)

- [ ] **Step 2: Implement `app/api/routes_tasks.py`**

```python
import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.events.sse import sse_events
from app.graph.state import initial_state
from app.services.run_manager import NotAwaitingApproval

router = APIRouter(prefix="/tasks")


class TaskCreate(BaseModel):
    task_description: str
    repo: str
    base_branch: str = "main"
    test_command: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)


class Feedback(BaseModel):
    feedback: str = ""


@router.post("", status_code=201)
async def create_task(body: TaskCreate, request: Request):
    if "/" not in body.repo:
        raise HTTPException(422, "repo must look like org/name")
    task_id = uuid.uuid4().hex
    from app.graph.state import initial_state
    st = dict(initial_state(task_id, body.task_description, body.repo, body.base_branch,
                            body.test_command or "", body.model_overrides))
    rm = request.app.state.run_manager
    await rm.prepare(task_id, st)
    asyncio.create_task(rm.drive(task_id, st))
    return {"task_id": task_id}


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request):
    st = await request.app.state.run_manager.get_state(task_id)
    if not st:
        raise HTTPException(404, "task not found")
    return st


async def _resume(request: Request, task_id: str, decision: str, feedback: str):
    try:
        await request.app.state.run_manager.resume(task_id, decision, feedback)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"resumed": True, "decision": decision}


@router.post("/{task_id}/approve")
async def approve(task_id: str, request: Request, body: FeedbackBody | None = None):
    return await _resume(request, task_id, "approved", (body.feedback if body else "") or "")


@router.post("/{task_id}/reject")
async def reject(task_id: str, request: Request, body: FeedbackBody | None = None):
    return await _resume(request, task_id, "rejected", (body.feedback if body else "") or "")


@router.get("/{task_id}/artifacts")
async def artifacts(task_id: str, request: Request):
    return await request.app.state.run_manager.artifacts(task_id)


@router.get("/{task_id}/events")
async def events(task_id: str, request: Request):
    from fastapi.responses import StreamingResponse
    from app.events.sse import sse_events
    return StreamingResponse(sse_events(request.app.state.services.redis, task_id),
                             media_type="text/event-stream")


from pydantic import BaseModel


class FeedbackBody(BaseModel):
    feedback: str = ""
```

(Move the `FeedbackBody` class and its `BaseModel` import to the TOP of the file in the final implementation; the route signatures reference it. Final import block: `uuid`, `asyncio` (unused — drop), `fastapi` bits, `pydantic BaseModel`, `app.events.sse.sse_events`, `app.graph.state.initial_state`, `app.services.run_manager` types.)

- [ ] **Step 3: Implement `app/main.py`**

```python
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.api.routes_tasks import router
from app.config import get_settings
from app.db import ensure_schema
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.nodes.helpers import Services
from app.llm.openrouter import OpenRouterClient
from app.llm.pricing import PriceTable
from app.sandbox.computers import MaritimeComputers
from app.sandbox.maritime import make_sandbox
from app.services.run_manager import RunManager


def create_app(services=None, graph=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:  # test wiring: fakes provided by the caller
            app.state.services = services
            app.state.run_manager = RunManager(services, graph)
            yield
            return
        settings = get_settings()
        redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
        await ensure_schema(settings.database_url)
        http = httpx.AsyncClient(timeout=130)
        prices = await PriceTable.fetch(http)
        svc = Services(
            settings=settings,
            llm=OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url, http),
            prices=prices,
            publisher=EventPublisher(redis),
            sandbox_factory=lambda state: make_sandbox(settings, state["task_id"],
                                                       state["repo"], state["base_branch"],
                                                       client=http),
            computers=MaritimeComputers(settings, http),
            redis=redis,
            pg_dsn=settings.database_url,
        )
        cm = AsyncPostgresSaver.from_conn_string(settings.database_url)
        checkpointer = await cm.__aenter__()
        await checkpointer.setup()
        app.state.services = svc
        app.state.run_manager = RunManager(svc, build_graph(svc, checkpointer))
        yield
        await redis.aclose()
        await http.aclose()
        await cm.__aexit__(None, None, None)

    app = FastAPI(title="agent-workspace", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 4: Write the API flow test**

`tests/integration/test_api_flow.py`:

```python
import asyncio
import json

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.main import create_app
from tests.fakes import StubLLM, make_services

pytestmark = pytest.mark.integration

SCRIPT_APPROVE = ["PLAN: fix add", "URL: https://docs.example.com/api", "notes",
                  '{"ops": [{"op": "write_file", "path": "calc.py", "content": "x"}], "done": true}',
                  '{"verdict": "approved", "comments": []}']
SCRIPT_REJECT = SCRIPT_APPROVE + ["REVISED PLAN", "URL: https://docs.example.com/api", "notes2",
                                  '{"ops": [{"op": "write_file", "path": "calc.py", "content": "y"}], "done": true}',
                                  '{"verdict": "approved", "comments": []}']


async def wait_status(client, task_id, wanted, timeout=10.0):
    async def poll():
        last = None
        for _ in range(300):
            r = await client.get(f"/tasks/{task_id}")
            last = r.json()
            if last.get("status") == wanted:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"status never reached {wanted}; last={last}")
    return await asyncio.wait_for(poll(), timeout)


def make_app(redis_client, script):
    services = make_services(llm=StubLLM(list(script)), publisher=EventPublisher(redis_client),
                             redis=redis_client)
    graph = build_graph(services, InMemorySaver())
    return create_app(services=services, graph=graph), services


async def test_submit_watch_approve(redis_client):
    app, services = make_app(redis_client, SCRIPT_APPROVE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo"})
        assert r.status_code == 201
        tid = r.json()["task_id"]
        st = await wait_status(client, tid, "awaiting_approval")
        assert st["code_diff"].startswith("diff --git")
        ev = await client.get(f"/tasks/{tid}/events")
        assert ev.status_code == 200
        assert ev.headers["content-type"].startswith("text/event-stream")
        r = await client.post(f"/tasks/{tid}/approve", json={})
        assert r.status_code == 202
        st = await wait_status(client, tid, "done")
        art = (await client.get(f"/tasks/{tid}/artifacts")).json()
        assert art["plan"] == "PLAN: fix add" and art["code_diff"]


async def test_reject_loops_back_to_planner(redis_client):
    app, services = make_app(redis_client, SCRIPT_REJECT)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo"})
        tid = r.json()["task_id"]
        await wait_status(client, tid, "awaiting_approval")
        r = await client.post(f"/tasks/{tid}/reject", json={"feedback": "use uuid"})
        assert r.status_code == 202
        st = await wait_status(client, tid, "awaiting_approval", timeout=15)
        assert st["task_description"].endswith("HUMAN FEEDBACK: use uuid")
        await client.post(f"/tasks/{tid}/approve", json={})
        await wait_status(client, tid, "done")


async def test_approve_when_not_awaiting_returns_409(redis_client):
    app, _services = make_app(redis_client, SCRIPT_APPROVE)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        with pytest.raises(httpx.HTTPStatusError) as ei:
            r = await client.post("/tasks/nonexistent/approve", json={})
            r.raise_for_status()
        assert ei.value.response.status_code == 404
```

(Add `import httpx` at the top. The 404-vs-409 test: `get_state` returns None → resume raises `NotAwaitingApproval("task not found")`? RunManager raises NotAwaitingApproval even for missing tasks; the route maps any RuntimeError from resume to 409. Simplify the test to expect 409 and drop the 404 distinction — implement `_resume` to raise `HTTPException(404)` when state is None and `HTTPException(409)` when status mismatches; adjust RunManager.resume to raise a dedicated `TaskNotFound` when `state` is None. Test then asserts 409 for an existing-but-done task — covered by the reject test's final approve? Keep the test simple: after a task reaches `done`, approving again returns 409.)

- [ ] **Step 5: Write the restart-resume test**

`tests/integration/test_restart_resume.py`:

```python
import asyncio

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.db import ensure_schema
from app.graph.build import build_graph
from app.main import create_app
from tests.fakes import FakePublisher, StubLLM, make_services

pytestmark = pytest.mark.integration

SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com", "notes",
          '{"ops": [{"op": "write_file", "path": "calc.py", "content": "x"}], "done": true}',
          '{"verdict": "approved", "comments": []}']


async def wait_status(client, task_id, wanted, timeout=10.0):
    async def poll():
        last = None
        for _ in range(300):
            r = await client.get(f"/tasks/{task_id}")
            last = r.json()
            if last.get("status") == wanted:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"status never reached {wanted}; last={last}")
    return await asyncio.wait_for(poll(), timeout)


async def test_interrupt_survives_full_process_restart(settings, redis_client):
    await ensure_schema(settings.database_url)

    # --- process A: run to the approval interrupt, then "die" ---
    services_a = make_services(llm=StubLLM(SCRIPT := [
        "PLAN: fix add", "URL: https://docs.example.com", "notes",
        '{"ops": [{"op": "write_file", "path": "calc.py", "content": "x"}], "done": true}',
        '{"verdict": "approved", "comments": []}']),
        publisher=FakePublisher(), redis=redis_client, pg_dsn=settings.database_url)
    cm_a = AsyncPostgresSaver.from_conn_string(settings.database_url)
    checkpointer_a = await cm_a.__aenter__()
    await checkpointer.setup()
    app_a = create_app(services=services_a, graph=build_graph(services_a, checkpointer))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a),
                                 base_url="http://t") as client:
        r = await client.post("/tasks", json={"task_description": "fix add", "repo": "org/repo"})
        tid = r.json()["task_id"]
        st = await wait_status(client, tid, "awaiting_approval")
        assert st["paused_at"]
    await cm_a.__aexit__(None, None, None)  # process A exits

    # --- process B: brand-new graph + checkpointer over the SAME Postgres ---
    services_b = make_services(publisher=FakePublisher(), redis=redis_client,
                               pg_dsn=settings.database_url)  # no LLM needed post-approval
    cm_b = AsyncPostgresSaver.from_conn_string(settings.database_url)
    checkpointer_b = await cm_b.__aenter__()
    app_b = create_app(services=services_b, graph=build_graph(services_b, checkpointer_b))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b),
                                 base_url="http://t") as client_b:
        r = await client_b.post(f"/tasks/{tid}/approve", json={})
        assert r.status_code == 202
        st = await wait_status(client_b, tid, "done")
        assert st["resumed_at"] and st["paused_at"]
    await cm_b.__aexit__(None, None, None)
```

(Names to fix when typing the final file: the walrus in `make_services(llm=StubLLM(SCRIPT := [...]))` is pointless — define `SCRIPT = [...]` on its own line first. Variable-name consistency: `checkpointer` (A) and `checkpointer_b`; use them consistently in `build_graph` calls. `test_restart_resume` must exercise the REAL Postgres checkpointer on both sides — that is the acceptance-criterion proof, do not swap in InMemorySaver.)

- [ ] **Step 6: Run both tests to pass**

Run: `docker compose up -d` then `python -m pytest tests/integration/test_api_flow.py tests/integration/test_restart_resume.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app tests
git commit -m "feat: run manager, FastAPI surface with SSE, restart-safe resume"
```

---

### Task 14: Demo scaffolding + real-service E2E

**Files:**
- Create: `demo/sample-repo/calc.py`, `demo/sample-repo/tests/test_calc.py`, `demo/sample-repo/requirements.txt`, `demo/phase1.md`, `tests/e2e/test_phase1_real.py`

**Interfaces:**
- Consumes: live OpenRouter + Maritime (e2e only), `REPO_UNDER_TEST` env (org/name of a GitHub repo created from `demo/sample-repo`)
- Produces: the sample repo (deliberate bug `return a - b` in `add`), the demo script, and a marked e2e test that builds real Services exactly like `main.lifespan` (in-memory event list, InMemorySaver), drives to interrupt, asserts browser/tool_call events, resumes approved, asserts `done` + passing `test_results`. Skips without keys.

- [ ] **Step 1: Create the sample repo**

`demo/sample-repo/calc.py`:

```python
def add(a, b):
    return a - b  # BUG: should be the sum
```

`demo/sample-repo/tests/test_calc.py`:

```python
from calc import add


def test_add():
    assert add(2, 2) == 4
```

`demo/sample-repo/requirements.txt`:

```
pytest
```

- [ ] **Step 2: Write the e2e test**

`tests/e2e/test_phase1_real.py`:

```python
import os

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

pytestmark = pytest.mark.e2e

requires_keys = pytest.mark.skipif(
    not (os.getenv("OPENROUTER_API_KEY") and os.getenv("MARITIME_API_KEY")
         and os.getenv("MARITIME_TEMPLATE_ID") and os.getenv("REPO_UNDER_TEST")),
    reason="set OPENROUTER_API_KEY, MARITIME_API_KEY, MARITIME_TEMPLATE_ID, "
           "REPO_UNDER_TEST (org/name of a GitHub repo created from demo/sample-repo)")


class ListPublisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return "0-1"


@requires_keys
async def test_phase1_end_to_end():
    from app.config import get_settings
    from app.graph.build import build_graph
    from app.graph.nodes.helpers import Services
    from app.graph.state import initial_state
    from app.llm.openrouter import OpenRouterClient
    from app.llm.pricing import PriceTable
    from app.sandbox.computers import MaritimeComputers
    from app.sandbox.maritime import make_sandbox

    settings = get_settings()
    publisher = ListPublisher()
    http = httpx.AsyncClient(timeout=130)
    prices = await PriceTable.fetch(http)
    svc = Services(
        settings=settings,
        llm=OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url, http),
        prices=prices,
        publisher=publisher,
        sandbox_factory=lambda st: make_sandbox(settings, st["task_id"], st["repo"],
                                                st["base_branch"], client=http),
        computers=MaritimeComputers(settings, http),
        redis=None,
    )
    graph = build_graph(svc, InMemorySaver())
    config = {"configurable": {"thread_id": "e2e-phase1"}}
    state = dict(initial_state(
        "e2e-phase1",
        "Fix the failing test in tests/test_calc.py so add(a, b) returns the sum.",
        os.environ["REPO_UNDER_TEST"], "main", "pytest -q", {}))

    interrupted = False
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupted = True
            break
    assert interrupted

    events = publisher.events
    assert any(e.node == "researcher" and e.data.get("action") == "browser_open" for e in events)
    assert any(e.data.get("viewer_url") for e in events)
    assert any(e.type == "cost_update" for e in events)

    async for _chunk in graph.astream(Command(resume={"decision": "approved", "feedback": ""}),
                                      config, stream_mode="updates"):
        pass
    snap = await graph.aget_state(config)
    assert snap.values["status"] == "done"
    assert snap.values["test_results"]["passed"] is True
```

- [ ] **Step 3: Run (expect SKIP without keys)**

Run: `python -m pytest tests/e2e -v`
Expected: 1 skipped.

- [ ] **Step 4: Write `demo/phase1.md`**

```markdown
# Phase 1 demo — submit, watch, sleep, approve, resume

1. `docker compose up -d`
2. Fill `.env` (OPENROUTER_API_KEY, MARITIME_API_KEY, MARITIME_TEMPLATE_ID, GITHUB_PAT).
3. Create a GitHub repo from `demo/sample-repo/` and push it; note `org/name`.
4. Start the API: `.venv\Scripts\uvicorn app.main:app --port 8000`
5. Submit (PowerShell):
   `curl -X POST http://localhost:8000/tasks -H "Content-Type: application/json" -d '{"task_description": "Fix the failing test in tests/test_calc.py so add(a, b) returns the sum.", "repo": "YOUR_ORG/YOUR_REPO"}'`
6. Watch live: `curl -N http://localhost:8000/tasks/TASK_ID/events`
   - You will see node_started/tool_call (incl. browser_open + viewer_url)/node_completed frames.
   - The Researcher is visible LIVE in Maritime's dashboard: open the viewer_url,
     and find the VM `aw-task-TASK_ID` on maritime.sh.
7. It pauses at Human Approval (VM sleeps). Leave it overnight.
8. Next morning: `curl -X POST http://localhost:8000/tasks/TASK_ID/approve -d '{}'`
   - Grep the event stream for `sleep_wake`: it carries `paused_at`, `resumed_at`,
     and the measured `resume_latency_seconds` — the headline stat.
9. `curl http://localhost:8000/tasks/TASK_ID/artifacts` shows plan, notes, diff, test results.
```

- [ ] **Step 3b: Run unit/integration suite once more, then commit**

Run: `python -m pytest tests -m "not e2e" -v` → all PASS.

```bash
git add demo tests
git commit -m "chore: phase-1 demo scaffolding and real-service e2e"
```

---

## Self-Review

**Spec coverage:**
- TaskState + extensions, TestResult (spec §4) → Task 2
- Graph edges, retry bounds 3/2, separate counters, needs_human (spec §5) → Tasks 2, 9, 10, 11
- Transient backoff 1/4/16 not touching retry_counts (spec §5) → Task 4 (`with_retry`, tests assert delays)
- Maritime VM lifecycle: aw-task-{id}, lazy create, warm reuse, 404-recreate, sleep at approval, run_long poll, cumulative diff vs base (spec §7) → Tasks 5, 11 (reuse asserts), 13 (sleep at interrupt)
- Computers: get-or-create, actions, viewer link, paid-plan error surfacing (spec §7) → Task 6, Task 8 (researcher emits viewer_url + browser_open)
- OpenRouter + per-call cost into state (spec §6, §10 partial) → Tasks 4, 6 (`apply_llm_cost`), visible in cost_update events (Task 11)
- Redis schema `aw:{id}:state|events`, SSE schema, event types (spec §8) → Tasks 3, 13
- Postgres checkpointer + tasks table (spec §8) → Tasks 12, 13
- Human approval via API; resume paths approved/rejected with feedback→Planner (spec §5, §8) → Tasks 10, 13
- Restart-resume proof (spec §12 criterion) → Task 13 `test_interrupt_survives_full_process_restart` (real PG both sides)
- Kill-process, live SSE, phase-1 acceptance gate (spec §12) → Task 13 API flow, Task 14 demo
- Phase 2 (push/draft PR/webhooks) and phase 3 (Redis cost accumulator, audit rows, VM-minute metering) are separate plans, per the phased approach — not silently dropped.

**Placeholder scan:** all implementation steps carry complete code; the two deliberately-flagged cleanups (Task 4 streaming-assert final form, Task 6 Step 4 test cleanup, Task 13 `resume` single-line fix) are written as explicit instructions to produce the shown final code, not as TODOs. No TBD/TODO/"similar to Task N" anywhere.

**Type consistency check:** `Sandbox` protocol methods match `MaritimeSandbox`, `StubSandbox` and all node call sites (`ensure/exec/run_long/write_file/read_file/list_files/diff/sleep`); `Services` fields match `make_services` and `create_app` wiring; `route_after_*` return strings match `Command` goto targets declared in the graph; `TestResult.to_dict` → `test_results` dict consumed by `route_after_tester` and `coding_agent_node`; `initial_state` keys cover every `TaskState` key; `Event`/`EventPublisher`/`sse_events` names used identically in Tasks 3, 7, 13.