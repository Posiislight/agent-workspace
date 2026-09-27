# Phase 3 — Cost Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every dollar is traceable to its task: LLM token cost per call lands in Redis (`INCRBYFLOAT aw:{id}:cost`) with a `cost_update` SSE event, VM awake-minutes accrue at a configurable nominal rate, and final totals reach the Postgres audit row, the final PR description, and the live UI.

**Architecture:** A new `CostTracker` service owns two Redis keys per task (`aw:{id}:cost` for LLM dollars, `aw:{id}:vm_cost`/`aw:{id}:vm_minutes` for VM metering). `RunManager.drive` opens/closes an "awake window" per drive (VM sleeps only at the approval pause), and `_mirror` merges tracker totals into the Redis/PG mirror so the API and audit row always show live totals. `commit_pr` re-renders the PR body with final totals and appends an LLM/VM breakdown to the final summary comment. `apply_llm_cost` feeds the tracker on every LLM call.

**Tech Stack:** Python 3.12, Redis via `redis.asyncio` (`INCRBYFLOAT`), asyncpg, FastAPI, LangGraph, React/Vite frontend (build via `npm run build`), existing fake-based pytest patterns.

**Spec:** `docs/superpowers/specs/2026-09-23-agent-workspace-design.md` — §10 (Phase 3 — Cost tracking), §8 (Redis schema `aw:{id}:cost`), §12 (phase gate: "cost visible live and in the final PR/audit row").

## Global Constraints

- No new dependencies: Redis commands via existing `redis.asyncio` client; time via stdlib `time`.
- The per-model price table already exists (`app/llm/pricing.py`, fetched once at startup in `app/main.py` lifespan). Do NOT re-fetch per call; the process-lifetime cache satisfies "fetched from OpenRouter's models endpoint, cached".
- The Redis tracker total is the source of truth for actual spend (it survives checkpoint replays after a crash/restart, where graph-state `cost_so_far` may undercount). Graph-state `cost_so_far` stays as-is for checkpointing.
- Never break the existing `cost_update` event contract: data stays exactly `{"cost_so_far": <total>}` (frontend + `tests/test_node_helpers.py` depend on it).
- Config only via `app/config.py` Settings (pydantic-settings, env file `.env`).
- Repo style: no comments unless needed, ruff-clean, async tests run under `asyncio_mode=auto` (no markers needed for unit tests).
- Run tests with: `.venv\Scripts\python.exe -m pytest tests/test_x.py -v` (Windows; the venv python is required — bare `python` has no pytest). Integration tests additionally need `docker compose up -d` and are marked `integration`.
- After frontend edits, rebuild with `cd app/frontend; npm run build` and commit `dist/`.

---

### Task 1: Settings + CostTracker (Redis INCRBYFLOAT)

**Files:**
- Modify: `app/config.py`
- Create: `app/services/cost_tracker.py`
- Modify: `tests/fakes.py` (add `FakeRedis.incrbyfloat`)
- Test: `tests/test_cost_tracker.py`

**Interfaces:**
- Consumes: `redis.asyncio` client (or `FakeRedis`) with `get`/`incrbyfloat`.
- Produces:

```python
# app/services/cost_tracker.py
class CostTracker:
    def __init__(self, redis, vm_rate_per_minute: float = 0.0): ...
    async def add_llm_cost(self, task_id: str, delta: float) -> float
        # INCRBYFLOAT aw:{task_id}:cost; returns new total (0.0 if redis is None)
    def vm_awake_start(self, task_id: str) -> None
    async def vm_awake_stop(self, task_id: str) -> float
        # closes the awake window opened by vm_awake_start; accrues
        # minutes * vm_rate_per_minute; returns the vm-cost delta (0.0 if no window)
    async def add_vm_minutes(self, task_id: str, minutes: float) -> float
        # INCRBYFLOAT aw:{task_id}:vm_minutes and aw:{task_id}:vm_cost; returns vm-cost delta
    async def snapshot(self, task_id: str) -> dict
        # {"llm_cost": float, "vm_minutes": float, "vm_cost": float}
```

Also: `Settings.vm_cost_per_minute: float = 0.0`, and `FakeRedis.incrbyfloat(key, amount) -> float` mirroring real redis semantics (creates the key at the given amount, returns the new value as float).

- [ ] **Step 1: Write the failing test**

`tests/test_cost_tracker.py`:

```python
from app.services.cost_tracker import CostTracker
from tests.fakes import FakeRedis


async def test_add_llm_cost_accumulates_in_redis():
    redis = FakeRedis()
    t = CostTracker(redis)
    assert await t.add_llm_cost("t1", 0.001) == 0.001
    assert await t.add_llm_cost("t1", 0.002) == 0.003
    assert float(redis.store["aw:t1:cost"]) == 0.003


async def test_add_llm_cost_without_redis_is_zero():
    t = CostTracker(None)
    assert await t.add_llm_cost("t1", 0.5) == 0.0


async def test_vm_awake_window_accrues_at_rate():
    import asyncio

    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_minute=60.0)
    t.vm_awake_start("t1")
    await asyncio.sleep(0.01)
    delta = await t.vm_awake_stop("t1")
    assert delta > 0
    assert 0 < float(redis.store["aw:t1:vm_cost"]) <= 0.1
    assert await t.vm_awake_stop("t1") == 0.0  # window already closed


async def test_vm_awake_stop_without_start_is_zero():
    t = CostTracker(FakeRedis())
    assert await t.vm_awake_stop("t1") == 0.0


async def test_add_vm_minutes_converts_rate_and_tracks_minutes():
    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_minute=6.0)
    delta = await t.add_vm_minutes("t1", 10.0)
    assert delta == 1.0
    assert float(redis.store["aw:t1:vm_minutes"]) == 10.0
    assert float(redis.store["aw:t1:vm_cost"]) == 1.0


async def test_snapshot_returns_all_components():
    redis = FakeRedis()
    t = CostTracker(redis, vm_rate_per_minute=6.0)
    await t.add_llm_cost("t1", 1.25)
    await t.add_vm_minutes("t1", 10.0)
    snap = await t.snapshot("t1")
    assert snap == {"llm_cost": 1.25, "vm_minutes": 10.0, "vm_cost": 1.0}
    assert await CostTracker(None).snapshot("t1") == {
        "llm_cost": 0.0, "vm_minutes": 0.0, "vm_cost": 0.0}
```

Append to `tests/test_config.py`:

```python
def test_vm_cost_settings_default():
    from app.config import Settings
    s = Settings(github_pat="p")
    assert s.vm_cost_per_minute == 0.0
```

Append the `incrbyfloat` method to `FakeRedis` in `tests/fakes.py` (inside the class, after `delete`):

```python
    async def incrbyfloat(self, key, amount):
        current = float(self.store.get(key, 0.0) or 0.0)
        current += float(amount)
        self.store[key] = repr(current)
        return current
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cost_tracker.py tests/test_config.py::test_vm_cost_settings_default -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.cost_tracker'` and unknown settings attr.

- [ ] **Step 3: Write minimal implementation**

`app/config.py` — after `merge_pr_when_ready: bool = False` add:

```python
    vm_cost_per_minute: float = 0.0
```

`app/services/cost_tracker.py`:

```python
import time


class CostTracker:
    """Per-task cost accumulator: LLM dollars + VM awake-minutes (spec §10)."""

    def __init__(self, redis, vm_rate_per_minute: float = 0.0):
        self._redis = redis
        self._rate = vm_rate_per_minute
        self._awake_since: dict[str, float] = {}

    async def add_llm_cost(self, task_id: str, delta: float) -> float:
        if self._redis is None:
            return 0.0
        return float(await self._redis.incrbyfloat(f"aw:{task_id}:cost", delta))

    def vm_awake_start(self, task_id: str) -> None:
        self._awake_since[task_id] = time.monotonic()

    async def vm_awake_stop(self, task_id: str) -> float:
        started = self._awake_since.pop(task_id, None)
        if started is None:
            return 0.0
        minutes = (time.monotonic() - started) / 60.0
        return await self.add_vm_minutes(task_id, minutes)

    async def add_vm_minutes(self, task_id: str, minutes: float) -> float:
        if self._redis is None:
            return 0.0
        await self._redis.incrbyfloat(f"aw:{task_id}:vm_minutes", minutes)
        vm_cost = round(minutes * self._rate, 6)
        if vm_cost:
            await self._redis.incrbyfloat(f"aw:{task_id}:vm_cost", vm_cost)
        return vm_cost

    async def snapshot(self, task_id: str) -> dict:
        if self._redis is None:
            return {"llm_cost": 0.0, "vm_minutes": 0.0, "vm_cost": 0.0}
        async def _f(key):
            raw = await self._redis.get(key)
            return float(raw) if raw else 0.0
        return {"llm_cost": await _f(f"aw:{task_id}:cost"),
                "vm_minutes": await _f(f"aw:{task_id}:vm_minutes"),
                "vm_cost": await _f(f"aw:{task_id}:vm_cost")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_cost_tracker.py tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/services/cost_tracker.py tests/fakes.py tests/test_cost_tracker.py tests/test_config.py
git commit -m "feat: CostTracker with Redis INCRBYFLOAT for LLM and VM costs"
```

---

### Task 2: Wire tracker into Services + apply_llm_cost

**Files:**
- Modify: `app/graph/nodes/helpers.py` (`Services` dataclass, `apply_llm_cost`)
- Modify: `tests/fakes.py` (`make_services` gains `cost=` param, auto-wires a `CostTracker` when `redis` is given)
- Test: `tests/test_node_helpers.py` (append)

**Interfaces:**
- Consumes: `CostTracker` (Task 1 — `add_llm_cost(task_id, delta) -> float`).
- Produces: `Services.cost: object = None` field (after `github`, before `clock`); `make_services(..., cost=None)` — when `cost is None` and `redis is not None`, it builds `CostTracker(redis, settings.vm_cost_per_minute)`. `apply_llm_cost(updates, state, services, result, node)` — same signature as before, additionally calls `tracker.add_llm_cost` when a tracker is present; event data stays exactly `{"cost_so_far": total}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_node_helpers.py`:

```python
async def test_apply_llm_cost_increments_redis_tracker():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    redis = FakeRedis()
    s = make_services(redis=redis)
    state = {"task_id": "t-cost", "cost_so_far": 0.0}
    updates = await apply_llm_cost({}, state, s, LLMResult("x", 100, 10, "m"), "planner")
    assert updates["cost_so_far"] == 0.001
    assert float(redis.store["aw:t-cost:cost"]) == 0.001


async def test_apply_llm_cost_without_tracker_still_works():
    from app.graph.nodes.helpers import apply_llm_cost
    from app.llm.openrouter import LLMResult

    s = make_services(cost=False)
    s.cost = None
    updates = await apply_llm_cost({}, {"task_id": "t", "cost_so_far": 0.0}, s,
                                   LLMResult("x", 100, 10, "m"), "planner")
    assert updates["cost_so_far"] == 0.001


async def test_make_services_autowires_tracker_from_redis():
    s = make_services(redis=FakeRedis())
    from app.services.cost_tracker import CostTracker
    assert isinstance(s.cost, CostTracker)
```

Note: `FakeRedis` must be imported at the top of the file (`from tests.fakes import FakeRedis, make_services`) — adjust the existing import line.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_node_helpers.py -v`
Expected: FAIL — `Services` has no attribute `cost` / `make_services` rejects `cost` kwarg.

- [ ] **Step 3: Write minimal implementation**

`app/graph/nodes/helpers.py` — `Services` gains a field (keep field order; `cost` after `github`):

```python
    pg_dsn: str | None = None
    github: object = None
    cost: object = None
    clock: Callable[[], str] = field(default_factory=lambda: now_iso)
```

`apply_llm_cost` becomes:

```python
async def apply_llm_cost(updates: dict, state, services, result, node: str) -> dict:
    delta = services.prices.cost(result.model, result.prompt_tokens, result.completion_tokens)
    total = round(state.get("cost_so_far", 0.0) + delta, 6)
    updates["cost_so_far"] = total
    tracker = getattr(services, "cost", None)
    if tracker is not None and delta:
        await tracker.add_llm_cost(state["task_id"], delta)
    await emit(services, {**state, "cost_so_far": total}, node, "cost_update",
               {"cost_so_far": total})
    return updates
```

`tests/fakes.py` — `make_services` signature and body:

```python
def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None,
                  github=None, cost=None) -> SimpleNamespace:
    """Build a Services namespace wired to fakes. sandbox_factory returns the shared stub."""
    from app.graph.nodes.helpers import Services
    from app.services.cost_tracker import CostTracker

    settings = settings or Settings()
    sandbox = sandbox or StubSandbox()
    if cost is None and redis is not None:
        cost = CostTracker(redis, settings.vm_cost_per_minute)
    return Services(
        settings=settings,
        llm=llm or StubLLM([]),
        prices=prices or StubPriceTable(),
        publisher=publisher or FakePublisher(),
        sandbox_factory=lambda state: sandbox,
        computers=computers or StubComputers(),
        redis=redis,
        pg_dsn=pg_dsn,
        github=github,
        cost=cost,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_node_helpers.py tests/test_cost_tracker.py -v`
Expected: PASS (the pre-existing `test_apply_llm_cost_updates_state_and_emits` still passes: `make_services()` without redis leaves `cost=None`).

- [ ] **Step 5: Commit**

```bash
git add app/graph/nodes/helpers.py tests/fakes.py tests/test_node_helpers.py
git commit -m "feat: apply_llm_cost feeds per-task Redis cost tracker"
```

---

### Task 3: VM awake-minutes in RunManager.drive + PG audit columns

**Files:**
- Modify: `app/services/run_manager.py` (`drive`, `_mirror`)
- Modify: `app/db.py` (DDL + `upsert_task` + `list_tasks`)
- Test: `tests/test_run_manager_vm_cost.py`, `tests/integration/test_db.py` (append)

**Interfaces:**
- Consumes: `CostTracker` (`vm_awake_start`, `vm_awake_stop`, `snapshot`).
- Produces: each `RunManager.drive` opens an awake window at entry and closes it in the interrupt branch (right before `sandbox.sleep()`) and in the `finally` block. `_mirror` merges `{"llm_cost", "vm_minutes", "vm_cost"}` from `tracker.snapshot(...)` into the Redis mirror values (only when tracker + redis exist) and passes `vm_cost=`/`vm_minutes=` to `upsert_task`. `upsert_task(dsn, task_id, repo, status, *, ..., vm_cost=None, vm_minutes=None)`; tasks table gains `vm_cost float8` and `vm_minutes float8`; `list_tasks` select gains `vm_cost`.

- [ ] **Step 1: Write the failing test**

`tests/test_run_manager_vm_cost.py`:

```python
import json

from app.graph.build import build_graph
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services
from tests.test_graph_scenarios import OPS, APPROVED, URLS
```

Stop — `tests/test_graph_scenarios.py` is an integration module (imports docker deps? no, but it's marked integration and lives in integration/). Do NOT import from it. Inline the script constants instead:

```python
import asyncio
import json

from langgraph.checkpoint.memory import InMemorySaver

from app.graph.build import build_graph
from app.graph.state import initial_state
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services

OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py",
                           "content": "x = 1"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com/api", "notes",
          OPS, APPROVED]


def _services(redis, rate=6.0):
    from app.config import Settings
    return make_services(llm=StubLLM(list(SCRIPT)), sandbox=StubSandbox(),
                         github=StubGitHub(), redis=redis,
                         settings=Settings(github_pat="p",
                                           vm_cost_per_minute=rate))


async def test_drive_accrues_vm_cost_and_mirrors_totals():
    from tests.fakes import FakeRedis
    redis = FakeRedis()
    services = _services(redis)
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    await rm.drive("t-vm", dict(initial_state("t-vm", "fix add", "org/repo",
                                              "main", "pytest -q", {})))
    snap = await services.cost.snapshot("t-vm")
    assert snap["llm_cost"] > 0          # 5 LLM calls (planner, researcher x2, coding, reviewer) * stub price
    assert snap["vm_minutes"] > 0
    assert snap["vm_cost"] > 0
    mirrored = json.loads(await redis.get("aw:t-vm:state"))
    assert mirrored["llm_cost"] == snap["llm_cost"]
    assert mirrored["vm_cost"] == snap["vm_cost"]
    assert mirrored["vm_minutes"] == snap["vm_minutes"]
    assert services.cost._awake_since == {}  # window closed at drive end
```

Append to `tests/integration/test_db.py` (file is `pytestmark = pytest.mark.integration`; fixture is `settings`):

```python
async def test_vm_cost_columns_roundtrip(settings):
    from app.db import ensure_schema, get_task, upsert_task
    await ensure_schema(settings.database_url)
    await upsert_task(settings.database_url, "t-vm2", "org/repo", "done",
                      cost_so_far=1.25, vm_cost=1.0, vm_minutes=10.0)
    row = await get_task(settings.database_url, "t-vm2")
    assert row["vm_cost"] == 1.0
    assert row["vm_minutes"] == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_run_manager_vm_cost.py tests/integration/test_db.py::test_vm_cost_columns_roundtrip -v -m integration`
Expected: FAIL — mirror has no `llm_cost` key; `upsert_task() got an unexpected keyword argument 'vm_cost'`.

- [ ] **Step 3: Write minimal implementation**

`app/db.py`:

- DDL gains, after `cost_so_far float8,`: `vm_cost float8,` and `vm_minutes float8,`.
- `ensure_schema` gains:

```python
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS vm_cost float8")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS vm_minutes float8")
```

- `upsert_task` signature gains `vm_cost=None, vm_minutes=None`; the INSERT becomes:

```python
        await conn.execute(
            """INSERT INTO tasks (task_id, repo, task_description, status, updated_at,
                                   paused_at, resumed_at, cost_so_far, retry_counts, pr_url,
                                   pr_number, vm_cost, vm_minutes)
               VALUES ($1, $2, $3, $4, now(), $5, $6, $7, $8::jsonb, $9, $10, $11, $12)
               ON CONFLICT (task_id) DO UPDATE SET
                 repo = $2, task_description = $3, status = $4, updated_at = now(),
                 paused_at = $5, resumed_at = $6, cost_so_far = $7,
                 retry_counts = $8::jsonb, pr_url = $9, pr_number = $10,
                 vm_cost = $11, vm_minutes = $12""",
            task_id, repo, description, status, _as_ts(paused_at), _as_ts(resumed_at),
            cost_so_far, json.dumps(retry_counts or {}), pr_url, pr_number,
            vm_cost, vm_minutes)
```

- `list_tasks` select becomes `... cost_so_far, vm_cost, pr_url`.

`app/services/run_manager.py`:

- In `drive`, right after the `lock_key` guard succeeds (before `config = ...`), add:

```python
        tracker = getattr(self.services, "cost", None)
        if tracker is not None:
            tracker.vm_awake_start(task_id)
```

- In the interrupt branch, immediately before `await self.services.sandbox_factory(values).sleep()`:

```python
                    if tracker is not None:
                        await tracker.vm_awake_stop(task_id)
```

- In the `finally` block, before the existing lock delete:

```python
            if tracker is not None:
                await tracker.vm_awake_stop(task_id)
```

(`vm_awake_stop` pops the window, so the double call at pause is a no-op — the interrupt branch already closed it.)

- `_mirror` becomes (snapshot merge ONLY into the Redis mirror — PG gets explicit
  `cost_so_far`/`vm_*` fields so the upsert stays a plain overwrite):

```python
    async def _mirror(self, task_id, values):
        if not values:
            return
        tracker = getattr(self.services, "cost", None)
        pg_values = values
        if tracker is not None and self.services.redis is not None:
            snap = await tracker.snapshot(task_id)
            await self.services.redis.set(
                f"aw:{task_id}:state",
                json.dumps({**values, "llm_cost": snap["llm_cost"],
                            "vm_cost": snap["vm_cost"],
                            "vm_minutes": snap["vm_minutes"]}))
            pg_values = {**values, "cost_so_far": snap["llm_cost"]}
        else:
            if self.services.redis is not None:
                await self.services.redis.set(f"aw:{task_id}:state", json.dumps(values))
        if self.services.pg_dsn:
            await upsert_task(self.services.pg_dsn, task_id, pg_values.get("repo", ""),
                              pg_values.get("status", ""),
                              description=pg_values.get("task_description"),
                              paused_at=pg_values.get("paused_at"),
                              resumed_at=pg_values.get("resumed_at"),
                              cost_so_far=pg_values.get("cost_so_far"),
                              retry_counts=pg_values.get("retry_counts"),
                              pr_url=pg_values.get("pr_url"),
                              pr_number=pg_values.get("pr_number"),
                              vm_cost=pg_values.get("vm_cost"),
                              vm_minutes=pg_values.get("vm_minutes"))
```

(When a tracker exists, `cost_so_far` in the audit row is the authoritative Redis
LLM total — checkpoint replays after a crash/restart may otherwise undercount.
`vm_cost`/`vm_minutes` are only ever written by `_mirror` when a snapshot exists;
without a tracker they stay None, which the columns accept.)

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose up -d; .venv\Scripts\python.exe -m pytest tests/test_run_manager_vm_cost.py tests/integration/test_db.py tests/test_run_manager_failures.py tests/test_run_manager_restart.py -v -m "integration or not e2e"`
Expected: PASS (existing failure-path tests exercise the new tracker code via `make_services(redis=FakeRedis())` auto-wiring).

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/services/run_manager.py tests/test_run_manager_vm_cost.py tests/integration/test_db.py
git commit -m "feat: VM awake-minute metering in drive loop + audit columns"
```

---

### Task 4: commit_pr final totals → PR description + summary breakdown

**Files:**
- Modify: `app/github/client.py` (add `update_pr_body`)
- Modify: `app/github/pr_flow.py` (add `render_final_body`)
- Modify: `app/graph/nodes/commit_pr.py`
- Modify: `tests/fakes.py` (`StubGitHub.update_pr_body` recording `updated_bodies`)
- Test: `tests/test_commit_pr.py` (append; read existing tests first — they assert exact summary content and must keep passing)

**Interfaces:**
- Consumes: `CostTracker.snapshot` (Task 1), `render_pr_body` (phase 2).
- Produces:

```python
# app/github/client.py
async def update_pr_body(self, repo: str, number: int, body: str) -> dict
    # PATCH /repos/{repo}/pulls/{number} {"body": body}

# app/github/pr_flow.py
def render_final_body(state: dict, totals: dict) -> str
    # render_pr_body with cost_so_far=total, plus a "## Final totals" section:
    # "- LLM: $X", "- VM: $Y (Z min awake)", "- Total: $T"

# app/graph/nodes/commit_pr.py — commit_pr_node(state, *, services) -> {"status": "done", "pr_url": ...}
```

Summary comment format (keeps existing assertions passing):

```
## Final summary
- Total cost: ${total:.2f}
- LLM: ${llm:.2f} · VM: ${vm:.2f} ({minutes:.1f} awake min)
- Retry cycles: testing: {t}, coding: {c}
[- Paused at: ..., resumed at: ...]
```

Where `total = llm + vm`, `llm = snapshot.llm_cost or state.cost_so_far`, `vm = snapshot.vm_cost or 0.0`, `minutes = snapshot.vm_minutes or 0.0`. When no tracker/redis is available (`snapshot` unavailable), fall back to state values only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_commit_pr.py` (imports at top already include `StubGitHub, StubSandbox, make_services`; add `FakeRedis` and `Settings` imports):

```python
async def test_commit_pr_updates_pr_body_with_final_totals():
    from app.config import Settings
    from tests.fakes import FakeRedis

    gh = StubGitHub()
    redis = FakeRedis()
    services = make_services(github=gh, sandbox=StubSandbox(), redis=redis,
                             settings=Settings(github_pat="p",
                                               vm_cost_per_minute=6.0))
    await services.cost.add_llm_cost("t1", 1.25)
    await services.cost.add_vm_minutes("t1", 10.0)
    state = {**_state(), "pr_url": "https://github.com/org/repo/pull/11",
             "pr_number": 11}
    result = await commit_pr_node(state, services=services)
    assert result == {"status": "done",
                      "pr_url": "https://github.com/org/repo/pull/11"}
    assert gh.updated_bodies and gh.updated_bodies[0][0] == 11
    body = gh.updated_bodies[0][1]
    assert "## Final totals" in body
    assert "$2.25" in body          # 1.25 LLM + 1.00 VM
    summary = gh.comments[0][1]
    assert "Total cost: $2.25" in summary
    assert "VM: $1.00 (10.0 awake min)" in summary


async def test_commit_pr_without_tracker_falls_back_to_state():
    gh = StubGitHub()
    services = make_services(github=gh, sandbox=StubSandbox())
    services.cost = None
    await commit_pr_node(_state(), services=services)
    summary = gh.comments[0][1]
    assert "Total cost: $1.25" in summary          # from state cost_so_far
    assert "awake min" in summary
    assert gh.updated_bodies[0][1].count("$1.25") >= 1
```

Note: `_state()` in that file already sets `cost_so_far: 1.25`, `retry_counts`, `paused_at`/`resumed_at`, and `task_description` — reuse it. The existing tests `test_commit_pr_pushes_marks_ready_and_comments` and `test_commit_pr_merges_when_configured` must pass unchanged (they now additionally get `updated_bodies`/VM lines — no assertion conflicts: none assert exact full-string equality of the comment).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_commit_pr.py -v`
Expected: FAIL — `StubGitHub` has no `updated_bodies`; `update_pr_body` missing.

- [ ] **Step 3: Write minimal implementation**

`app/github/client.py` — add after `mark_ready`:

```python
    async def update_pr_body(self, repo: str, number: int, body: str) -> dict:
        return await self._request("PATCH", f"/repos/{repo}/pulls/{number}",
                                   json={"body": body})
```

`tests/fakes.py` — `StubGitHub.__init__` gains `self.updated_bodies: list[tuple[int, str]] = []` (after `self.ready`), and the method:

```python
    async def update_pr_body(self, repo, number, body):
        self.updated_bodies.append((number, body))
        return {}
```

`app/github/pr_flow.py` — add:

```python
def render_final_body(state: dict, totals: dict) -> str:
    body = render_pr_body({**state, "cost_so_far": totals.get("total", 0.0)})
    return "\n".join([
        body, "",
        "## Final totals",
        f"- LLM: ${totals.get('llm_cost', 0.0):.2f}",
        f"- VM: ${totals.get('vm_cost', 0.0):.2f} "
        f"({totals.get('vm_minutes', 0.0):.1f} min awake)",
        f"- Total: ${totals.get('total', 0.0):.2f}",
    ])
```

`app/graph/nodes/commit_pr.py` — full rewrite:

```python
from app.github.pr_flow import ensure_draft_pr, render_final_body
from app.github.push import branch_for, push_branch
from app.graph.nodes.helpers import emit


async def _totals(services, state) -> dict:
    tracker = getattr(services, "cost", None)
    if tracker is None or services.redis is None:
        llm = state.get("cost_so_far", 0.0)
        return {"llm_cost": llm, "vm_cost": 0.0, "vm_minutes": 0.0,
                "total": round(llm, 6)}
    snap = await tracker.snapshot(state["task_id"])
    return {**snap, "total": round(snap["llm_cost"] + snap["vm_cost"], 6)}


async def commit_pr_node(state, *, services):
    """Finalize (reachable only from an approved HumanApproval): push, mark
    ready (or merge per config), write final totals into the PR description,
    append the summary comment, mark done."""
    await emit(services, state, "commit_pr", "node_started", {})
    github = services.github
    repo = state["repo"]
    branch = branch_for(state["task_id"])
    await push_branch(services.sandbox_factory(state), repo, branch,
                      services.settings.github_pat)
    if state.get("pr_number"):
        pr = {"number": state["pr_number"], "html_url": state["pr_url"],
              "draft": True}
    else:
        pr = await ensure_draft_pr(services, state)
    totals = await _totals(services, state)
    await github.update_pr_body(repo, pr["number"], render_final_body(state, totals))
    if services.settings.merge_pr_when_ready:
        await github.merge_pr(repo, pr["number"])
    else:
        await github.mark_ready(repo, pr["number"])
    retries = state.get("retry_counts") or {}
    summary = "\n".join([
        "## Final summary",
        f"- Total cost: ${totals['total']:.2f}",
        f"- LLM: ${totals['llm_cost']:.2f} · VM: ${totals['vm_cost']:.2f} "
        f"({totals['vm_minutes']:.1f} awake min)",
        f"- Retry cycles: testing: {retries.get('testing', 0)}, "
        f"coding: {retries.get('coding', 0)}",
    ])
    if state.get("paused_at") and state.get("resumed_at"):
        summary += f"\n- Paused at: {state['paused_at']}, resumed at: {state['resumed_at']}"
    await github.add_comment(repo, pr["number"], summary)
    await emit(services, state, "commit_pr", "node_completed",
               {"pr_url": pr["html_url"]})
    return {"status": "done", "pr_url": pr["html_url"]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_commit_pr.py tests/test_pr_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/github/client.py app/github/pr_flow.py app/graph/nodes/commit_pr.py tests/fakes.py tests/test_commit_pr.py
git commit -m "feat: commit_pr writes final cost totals into PR description and summary"
```

---

### Task 5: Lifespan wiring + end-to-end cost roundtrip (integration)

**Files:**
- Modify: `app/main.py` (build `CostTracker` in lifespan, pass `cost=` to `Services`)
- Create: `tests/integration/test_cost_roundtrip.py`

**Interfaces:**
- Consumes: everything above.
- Produces: production wiring where `services.cost` is a real `CostTracker(redis, settings.vm_cost_per_minute)`; integration proof of the phase gate: Redis keys grow per LLM call, mirror exposes totals, audit row has `vm_cost`/`vm_minutes`, final PR body carries totals.

- [ ] **Step 1: Write the failing test**

`tests/integration/test_cost_roundtrip.py` (follow `tests/integration/test_webhook_roundtrip.py` construction idioms — same fixtures `settings`/`redis_client`, same wait helpers, same webhook headers):

```python
import asyncio
import hashlib
import hmac as hmac_mod
import json
import uuid

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.config import Settings
from app.db import ensure_schema, get_task
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.state import initial_state
from app.main import create_app
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services

pytestmark = pytest.mark.integration

SECRET = "whsec"
OPS = json.dumps({"ops": [{"op": "write_file", "path": "calc.py",
                           "content": "x = 1"}], "done": True})
APPROVED = json.dumps({"verdict": "approved", "comments": []})
SCRIPT = ["PLAN: fix add", "URL: https://docs.example.com/api", "notes",
          OPS, APPROVED]


def _state(tid):
    return dict(initial_state(tid, "add a feature", "org/repo", "main",
                              "pytest -q", {}))


async def wait_for(predicate, timeout=10.0, msg="condition never met"):
    async def poll():
        last = None
        for _ in range(300):
            last = await predicate()
            if last:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"{msg}; last={last!r}")
    return await asyncio.wait_for(poll(), timeout)


async def test_cost_totals_reach_redis_pr_and_audit_row(settings, redis_client):
    await ensure_schema(settings.database_url)
    tid = f"t-cost-{uuid.uuid4().hex[:8]}"
    github = StubGitHub()
    services = make_services(
        llm=StubLLM(list(SCRIPT)), sandbox=StubSandbox(), github=github,
        publisher=EventPublisher(redis_client), redis=redis_client,
        settings=Settings(github_pat="p", github_webhook_secret=SECRET,
                          vm_cost_per_minute=6.0))
    services.pg_dsn = settings.database_url
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)
    app = create_app(services=services, graph=graph)
    app.state.run_manager = rm

    await rm.prepare(tid, _state(tid))
    await rm.start(tid, _state(tid))
    st = await wait_for(lambda: _status(rm, tid, "awaiting_approval"),
                        msg="never reached awaiting_approval")

    snap = await services.cost.snapshot(tid)
    assert snap["llm_cost"] > 0
    assert snap["vm_minutes"] > 0 and snap["vm_cost"] > 0
    live = await redis_client.get(f"aw:{tid}:cost")
    assert float(live) == snap["llm_cost"]
    assert st["vm_cost"] == snap["vm_cost"]

    body = json.dumps({"action": "submitted", "pull_request": {"number": 11},
                       "review": {"state": "approved", "body": ""},
                       "repository": {"full_name": "org/repo"}}).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac_mod.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": "pull_request_review",
        "content-type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        r = await client.post("/webhooks/github", content=body, headers=headers)
        assert r.status_code == 202, r.json()

    st = await wait_for(lambda: _status(rm, tid, "done"), msg="never reached done")
    assert github.updated_bodies and github.updated_bodies[0][0] == 11
    assert "## Final totals" in github.updated_bodies[0][1]
    summary = github.comments[0][1]
    assert "awake min" in summary

    row = await get_task(settings.database_url, tid)
    assert row["vm_cost"] > 0 and row["vm_minutes"] > 0
    assert row["cost_so_far"] > 0

    events = await redis_client.xrange(f"aw:{tid}:events", min="-", max="+")
    cost_events = [e for e in events if e[1].get("type") == "cost_update"]
    # planner (1) + researcher (2 calls) + coding_agent (1) + reviewer (1) = 5
    assert len(cost_events) == 5


def _status(rm, tid, wanted):
    async def get():
        s = await rm.get_state(tid)
        return s if s and s.get("status") == wanted else None
    return get
```



- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose up -d; .venv\Scripts\python.exe -m pytest tests/integration/test_cost_roundtrip.py -v -m integration`
Expected: FAIL — `services.cost` is None in production wiring (`main.py` doesn't build a tracker yet), so snapshot zeros / mirror lacks keys.

- [ ] **Step 3: Write minimal implementation**

`app/main.py` — in the lifespan, after `prices = await PriceTable.fetch(http_open)` add:

```python
        cost = CostTracker(redis, settings.vm_cost_per_minute)
```

add the import at top:

```python
from app.services.cost_tracker import CostTracker
```

and pass `cost=cost,` to the `Services(...)` constructor (after `github=` — there is none in main, so after `pg_dsn=settings.database_url`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/integration/test_cost_roundtrip.py tests/integration/test_webhook_roundtrip.py -v -m integration`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/integration/test_cost_roundtrip.py
git commit -m "feat: wire CostTracker into app lifespan with e2e cost roundtrip"
```

---

### Task 6: Live cost in the UI + docs + full verification

**Files:**
- Modify: `app/frontend/src/api/types.ts`
- Modify: `app/frontend/src/pages/TaskDetailPage.tsx`
- Modify: `app/frontend/src/pages/TaskListPage.tsx`
- Modify: `.env.example`
- Modify: `README.md`
- Test: none new (frontend has no test rig; verification = `npm run build` + full pytest + ruff)

**Interfaces:**
- Consumes: API mirror now includes `vm_cost`, `vm_minutes`, `llm_cost` (Task 3); `list_tasks` rows include `vm_cost` (Task 3).
- Produces: operator-facing live cost display.

- [ ] **Step 1: Frontend changes**

`app/frontend/src/api/types.ts` — `TaskState` gains (after `pr_url`):

```ts
  vm_cost?: number;
  vm_minutes?: number;
  llm_cost?: number;
```

`TaskSummary` gains (after `cost_so_far`):

```ts
  vm_cost?: number;
```

`app/frontend/src/pages/TaskDetailPage.tsx` — in the `detail-head` div, after the PR link block:

```tsx
        {task.cost_so_far > 0 && (
          <span className="chip cost" title="LLM cost so far">
            ${task.cost_so_far.toFixed(4)}
          </span>
        )}
        {(task.vm_cost ?? 0) > 0 && (
          <span className="chip cost" title="VM awake-time cost">
            +${task.vm_cost!.toFixed(4)} VM ({task.vm_minutes?.toFixed(1) ?? "0"} min)
          </span>
        )}
```

`app/frontend/src/pages/TaskListPage.tsx` — add a `<th>Cost</th>` after `<th>Stage</th>` and a cell after the stage `<td>`:

```tsx
            <td className="muted mono">
              {typeof t.cost_so_far === "number" ? `$${t.cost_so_far.toFixed(4)}` : "—"}
            </td>
```

- [ ] **Step 2: Build the frontend**

Run (PowerShell): `Set-Location app\frontend; npm run build; Set-Location ..\..`
Expected: `dist/` regenerated, no TS errors.

- [ ] **Step 3: Docs**

`.env.example` — append:

```
VM_COST_PER_MINUTE=0          # nominal $/min for VM awake time (phase 3 metric)
```

`README.md` — add a "Phase 3 — Cost tracking" section documenting:
- Per-call LLM cost: OpenRouter usage × price table → `INCRBYFLOAT aw:{id}:cost` + `cost_update` SSE event; price table is fetched once at startup from `/models`.
- VM awake-minutes: each graph drive opens an awake window; paused approval time does not accrue; minutes × `VM_COST_PER_MINUTE` land in `aw:{id}:vm_minutes`/`aw:{id}:vm_cost`.
- Final totals: written to the tasks table (`vm_cost`, `vm_minutes` columns) and into the final PR description + summary comment (LLM/VM breakdown).
- Where to see it live: task detail page cost chips (SSE-driven refresh), `GET /tasks/{id}` (`llm_cost`, `vm_cost`, `vm_minutes`), Redis key `aw:{id}:cost`.

- [ ] **Step 4: Full verification**

Run: `.venv\Scripts\python.exe -m pytest -v -m "not e2e"` then `.venv\Scripts\python.exe -m ruff check app tests`
Expected: all PASS, ruff clean (pre-existing lint debt in untouched files may be listed — do not fix files this plan doesn't touch; only ensure your own files are clean).

- [ ] **Step 5: Commit**

```bash
git add app/frontend/src app/frontend/dist .env.example README.md
git commit -m "feat: live cost display in UI and phase-3 docs"
```

---

## Phase-3 gate checklist (from spec §10/§12)

- [ ] LLM cost per call: usage × price table → `INCRBYFLOAT aw:{id}:cost` + `cost_update` SSE — Task 2
- [ ] Price table fetched from OpenRouter `/models`, cached — already built (app/main.py lifespan); verified in Task 5
- [ ] VM/computer awake-minutes at configurable nominal rate — Tasks 1/3
- [ ] Final totals → Postgres audit row — Task 3
- [ ] Final totals → final PR description + summary breakdown — Task 4
- [ ] Cost visible live (SSE + detail page + list column) — Task 6
- [ ] Every dollar traceable to its task (end-to-end roundtrip) — Task 5
