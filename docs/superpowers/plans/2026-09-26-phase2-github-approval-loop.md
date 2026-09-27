# Phase 2 — GitHub Approval Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At the HumanApproval interrupt, push the task branch and open a draft PR; approve/reject arrive via GitHub webhooks; the CommitAndPR node finalizes the PR with cost + retry summary.

**Architecture:** New `app/github/` package: an httpx-based GitHub REST client, a sandbox-exec branch pusher, and an idempotent "ensure draft PR" flow called from the existing `human_approval` node before `interrupt()`. A new `/webhooks/github` route verifies HMAC-SHA256 signatures, classifies `pull_request_review` / `issue_comment` / `pull_request ready_for_review` events into a resume decision, resolves the task via a new `pr_number` column in the tasks table, and calls the existing `RunManager.resume()`. The `commit_pr` node is upgraded from a stub to a real finalizer (push, mark ready or merge, summary comment).

**Tech Stack:** Python 3.12, FastAPI, httpx (already a dependency — no new deps; HMAC via stdlib `hmac`), LangGraph interrupts, Postgres via asyncpg, existing fake-based pytest patterns.

**Spec:** `docs/superpowers/specs/2026-09-23-agent-workspace-design.md` — sections 8 (webhook route), 9 (Phase 2), 12 (phase gates).

## Global Constraints

- No new dependencies: use `httpx` (>=0.27, already present) and stdlib `hmac`/`hashlib` for signature checks. Do NOT add PyNaCl/GitPython.
- Never log or persist the PAT. Error messages from push/exec must scrub it (`replace(pat, "***")`).
- All GitHub PR work goes through `app/github/client.py` (REST API); all git work happens inside the Maritime VM via `sandbox.run_long/exec` (Phase-1 pattern: `run_long` for anything possibly >110s).
- Webhook resume MUST go through `RunManager.resume(task_id, decision, feedback)` — never raw `Command(resume=...)` (run_manager.py:84-88 comment explains why).
- PR creation must be idempotent across interrupt re-runs: guard by looking up an existing open PR with the same head branch on GitHub (state updates made before `interrupt()` are NOT checkpointed, so local state cannot be the guard).
- Config only via `app/config.py` Settings (pydantic-settings, env file `.env`).
- Python strings/comments follow existing repo style (no comments unless needed, ruff-clean).
- Run tests with: `python -m pytest tests/test_x.py -v` (integration tests additionally need `docker compose up -d`, marked `integration`; e2e marked `e2e`).

---

### Task 1: Settings + GitHub REST client

**Files:**
- Modify: `app/config.py`
- Create: `app/github/__init__.py`
- Create: `app/github/client.py`
- Test: `tests/test_github_client.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Settings.github_webhook_secret: str = ""`, `Settings.merge_pr_when_ready: bool = False`; `class GitHubClient` with methods below. All return plain dicts parsed from GitHub JSON, raise `httpx.HTTPStatusError` on 4xx/5xx (via `raise_for_status`).

```python
class GitHubClient:
    def __init__(self, token: str, base_url: str = "https://api.github.com"): ...
    async def find_open_pr(self, repo: str, head: str) -> dict | None
        # GET /repos/{repo}/pulls?state=open&head={head}  (head = "owner:branch")
        # returns first matching PR dict or None
    async def create_pr(self, repo: str, *, title: str, body: str, head: str,
                        base: str, draft: bool = True) -> dict
        # POST /repos/{repo}/pulls  -> {"number": int, "html_url": str, "draft": bool, ...}
    async def mark_ready(self, repo: str, number: int) -> dict
        # PATCH /repos/{repo}/pulls/{number}  {"draft": False}
    async def add_comment(self, repo: str, number: int, body: str) -> dict
        # POST /repos/{repo}/issues/{number}/comments
    async def merge_pr(self, repo: str, number: int) -> dict
        # PUT /repos/{repo}/pulls/{number}/merge  {"merge_method": "squash"}
    async def aclose(self) -> None
```

- [ ] **Step 1: Write the failing test**

`tests/test_github_client.py`:

```python
import json

import httpx
import pytest

from app.github.client import GitHubClient


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_find_open_pr_matches_head():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.path == "/repos/org/repo/pulls"
        assert request.url.params["state"] == "open"
        assert request.url.params["head"] == "org:feature"
        assert request.headers["Authorization"] == "Bearer tok123"
        return httpx.Response(200, json=[{"number": 7, "html_url": "u", "draft": True}])

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(transport=_transport(handler),
                                   base_url="https://api.github.com")
    try:
        pr = await gh.find_open_pr("org/repo", "org:feature")
    finally:
        await gh.aclose()
    assert pr == {"number": 7, "html_url": "u", "draft": True}
    assert calls[0].startswith("https://api.github.com/repos/org/repo/pulls")


@pytest.mark.anyio
async def test_find_open_pr_none_when_empty():
    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(lambda req: httpx.Response(200, json=[])),
        base_url="https://api.github.com")
    try:
        assert await gh.find_open_pr("org/repo", "org:feature") is None
    finally:
        await gh.aclose()


@pytest.mark.anyio
async def test_create_pr_sends_draft_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen = json.loads(request.content)
        return httpx.Response(201, json={"number": 3, "html_url": "url", "draft": True})

    gh = GitHubClient("tok123")
    gh._client = httpx.AsyncClient(
        transport=_transport(handler), base_url="https://api.github.com")
    try:
        pr = await gh.create_pr("org/repo", title="T", body="B",
                                head="org:aw/x", base="main", draft=True)
    finally:
        await gh.aclose()
    assert pr["number"] == 3 and pr["draft"] is True
    assert seen == {"title": "T", "body": "B", "head": "aw/x", "base": "main",
                    "draft": True}
```

Note the `head` sent to GitHub is the bare branch (`aw/x`); the `owner:branch` form is only used as the `find_open_pr` query param. Write the test exactly as above (the handler asserting `{"head": "aw/x", ...}` locks that in).

Also add config tests to `tests/test_config.py` (append):

```python
def test_github_phase2_settings_defaults():
    from app.config import Settings
    s = Settings(github_pat="p", github_webhook_secret="s")
    assert s.github_webhook_secret == "s"
    assert s.merge_pr_when_ready is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_github_client.py tests/test_config.py::test_github_phase2_settings_defaults -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.github'` and unknown settings attr.

- [ ] **Step 3: Write minimal implementation**

`app/github/__init__.py` — empty file.

`app/github/client.py`:

```python
import httpx


class GitHubClient:
    """Minimal GitHub REST client for PR lifecycle (PAT auth)."""

    def __init__(self, token: str, base_url: str = "https://api.github.com"):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def find_open_pr(self, repo: str, head: str) -> dict | None:
        pulls = await self._request(
            "GET", f"/repos/{repo}/pulls",
            params={"state": "open", "head": head})
        return pulls[0] if pulls else None

    async def create_pr(self, repo: str, *, title: str, body: str, head: str,
                        base: str, draft: bool = True) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": head,
                  "base": base, "draft": draft})

    async def mark_ready(self, repo: str, number: int) -> dict:
        return await self._request(
            "PATCH", f"/repos/{repo}/pulls/{number}", json={"draft": False})

    async def add_comment(self, repo: str, number: int, body: str) -> dict:
        return await self._request(
            "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    async def merge_pr(self, repo: str, number: int) -> dict:
        return await self._request(
            "PUT", f"/repos/{repo}/pulls/{number}/merge",
            json={"merge_method": "squash"})

    async def aclose(self) -> None:
        await self._client.aclose()
```

`app/config.py` — after `github_pat: str = ""` (line 14) add:

```python
    github_webhook_secret: str = ""
    merge_pr_when_ready: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_github_client.py tests/test_config.py -v`
Expected: PASS (note: check how existing async tests run — `tests/conftest.py` sets a selector event loop; if `pytest.mark.anyio` has no anyio plugin configured, instead use `@pytest.mark.asyncio` matching the marker style used in e.g. `tests/test_maritime_sandbox.py` — follow whatever that file uses.)

- [ ] **Step 5: Commit**

```bash
git add app/github/__init__.py app/github/client.py app/config.py tests/test_github_client.py tests/test_config.py
git commit -m "feat: GitHub REST client and phase-2 settings"
```

---

### Task 2: Branch pusher (git in the VM)

**Files:**
- Create: `app/github/push.py`
- Test: `tests/test_push.py`

**Interfaces:**
- Consumes: `Sandbox` protocol (`app/sandbox/base.py` — `run_long(command) -> ExecResult` with `.exit_code/.stdout/.stderr`), `Settings.github_pat`.
- Produces:

```python
def branch_for(task_id: str) -> str          # "aw/{task_id}"
async def push_branch(sandbox, repo: str, branch: str, pat: str) -> None
    # raises PushError on non-zero exit; error text never contains pat
class PushError(RuntimeError)
```

- [ ] **Step 1: Write the failing test**

`tests/test_push.py`:

```python
import pytest

from app.github.push import PushError, branch_for, push_branch
from app.sandbox.base import ExecResult
from tests.fakes import StubSandbox


def test_branch_for_uses_aw_prefix():
    assert branch_for("abc123") == "aw/abc123"


@pytest.mark.anyio
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


@pytest.mark.anyio
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_push.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.github.push'`

- [ ] **Step 3: Write minimal implementation**

`app/github/push.py`:

```python
class PushError(RuntimeError):
    pass


def branch_for(task_id: str) -> str:
    return f"aw/{task_id}"


async def push_branch(sandbox, repo: str, branch: str, pat: str) -> None:
    url = f"https://x-access-token:{pat}@github.com/{repo}.git"
    cmd = (
        "cd /data/workspace && git add -A && "
        "(git diff --cached --quiet || git commit -m 'aw: task changes') && "
        f"git push {url} HEAD:refs/heads/{branch} && "
        f"git remote set-url origin https://github.com/{repo}.git"
    )
    result = await sandbox.run_long(cmd)
    if result.exit_code != 0:
        raise PushError(result.combined.replace(pat, "***"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_push.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/github/push.py tests/test_push.py
git commit -m "feat: branch pusher for task VMs"
```

---

### Task 3: StubGitHub fake + PR body rendering + ensure_draft_pr

**Files:**
- Modify: `tests/fakes.py` (add `StubGitHub`, add `github=` param to `make_services`)
- Modify: `app/graph/nodes/helpers.py` (add `github` field to `Services`)
- Create: `app/github/pr_flow.py`
- Test: `tests/test_pr_flow.py`

**Interfaces:**
- Consumes: `GitHubClient` interface (Task 1), `push_branch`/`branch_for` (Task 2).
- Produces:

```python
# app/github/pr_flow.py
def render_pr_body(state: dict) -> str
async def ensure_draft_pr(services, state) -> dict
    # returns {"number": int, "html_url": str, "draft": bool}
    # idempotent: returns existing open PR for head branch if one exists
```

`StubGitHub` (in `tests/fakes.py`) implements the same five methods as `GitHubClient` and records calls:

```python
class StubGitHub:
    def __init__(self, existing_prs=None, created=None):
        self.existing_prs = list(existing_prs or [])   # head -> pr dict map keyed by head string
        self.created = created or {"number": 11, "html_url": "https://github.com/org/repo/pull/11", "draft": True}
        self.created_prs: list[dict] = []
        self.comments: list[tuple[int, str]] = []
        self.ready: list[int] = []
        self.merged: list[int] = []

    async def find_open_pr(self, repo, head):
        return self.existing_prs.get(head)

    async def create_pr(self, repo, *, title, body, head, base, draft=True):
        self.created_prs.append({"title": title, "body": body, "head": head,
                                 "base": base, "draft": draft})
        return self.created

    async def mark_ready(self, repo, number):
        self.ready.append(number)
        return {}

    async def add_comment(self, repo, number, body):
        self.comments.append((number, body))
        return {}

    async def merge_pr(self, repo, number):
        self.merged.append(number)
        return {}

    async def aclose(self):
        pass
```

`make_services` gains `github=None` parameter, passed as `github=github` into `Services`. `Services` dataclass gains field `github: object = None` (after `pg_dsn`, before `clock`).

- [ ] **Step 1: Write the failing test**

`tests/test_pr_flow.py`:

```python
import pytest

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


@pytest.mark.anyio
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


@pytest.mark.anyio
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pr_flow.py -v`
Expected: FAIL — no module `app.github.pr_flow`, no `github` kwarg on `make_services`.

- [ ] **Step 3: Write minimal implementation**

In `app/graph/nodes/helpers.py` change `Services` to (only the new field shown; keep the rest):

```python
    pg_dsn: str | None = None
    github: object = None
    clock: Callable[[], str] = field(default_factory=lambda: now_iso)
```

In `tests/fakes.py` add `StubGitHub` as specified above and change `make_services` signature to:

```python
def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None,
                  github=None) -> SimpleNamespace:
```

and pass `github=github` into the `Services(...)` construction.

`app/github/pr_flow.py`:

```python
from app.github.push import branch_for, push_branch


def render_pr_body(state: dict) -> str:
    retries = state.get("retry_counts") or {}
    lines = [
        "## Task",
        state.get("task_description", ""),
        "",
        "## Plan",
        state.get("plan") or "(no plan captured)",
        "",
        "## Test results",
        str(state.get("test_results") or "(none)"),
        "",
        "## Reviewer comments",
        "\n".join(state.get("review_comments") or []) or "(none)",
        "",
        "## Run stats",
        f"- Running cost: ${state.get('cost_so_far', 0.0):.2f}",
        f"- Retry cycles: testing: {retries.get('testing', 0)}, "
        f"coding: {retries.get('coding', 0)}",
    ]
    paused = state.get("paused_at")
    if paused:
        lines.append(f"- Paused at: {paused}")
    return "\n".join(lines)


async def ensure_draft_pr(services, state) -> dict:
    repo = state["repo"]
    branch = branch_for(state["task_id"])
    head = f"{repo.split('/')[0]}:{branch}"
    existing = await services.github.find_open_pr(repo, head)
    if existing:
        return {"number": existing["number"], "html_url": existing["html_url"],
                "draft": existing.get("draft", True)}
    await push_branch(services.sandbox_factory(state), repo, branch,
                      services.settings.github_pat)
    pr = await services.github.create_pr(
        repo, title=f"aw: {state['task_description'][:60]}",
        body=render_pr_body(state), head=branch, base=state["base_branch"],
        draft=True)
    return {"number": pr["number"], "html_url": pr["html_url"],
            "draft": pr.get("draft", True)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pr_flow.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/github/pr_flow.py app/graph/nodes/helpers.py tests/fakes.py tests/test_pr_flow.py
git commit -m "feat: idempotent draft-PR flow with PR body rendering"
```

---

### Task 4: DB — pr_number column + lookup by PR

**Files:**
- Modify: `app/db.py`
- Modify: `app/services/run_manager.py` (`_mirror`)
- Modify: `app/graph/state.py` (add `pr_number`)
- Test: `tests/integration/test_db.py`

**Interfaces:**
- Consumes: existing `upsert_task`/`get_task`/`ensure_schema`.
- Produces: `upsert_task(..., *, pr_number=None)`, `get_task_by_pr(dsn, repo, pr_number) -> dict | None` (returns newest matching open task row). `TaskState` gains `pr_number: Optional[int]`. `RunManager._mirror` passes `pr_number=values.get("pr_number")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_db.py` (mark with the same integration marker style used in that file):

```python
@pytest.mark.integration
async def test_pr_number_roundtrip_and_lookup(pg_dsn):
    from app.db import ensure_schema, get_task_by_pr, upsert_task
    await ensure_schema(pg_dsn)
    await upsert_task(pg_dsn, "t-pr", "org/repo", "awaiting_approval",
                      description="d", pr_url="https://github.com/org/repo/pull/9",
                      pr_number=9)
    row = await get_task_by_pr(pg_dsn, "org/repo", 9)
    assert row is not None
    assert row["task_id"] == "t-pr"
    assert row["pr_number"] == 9
    assert await get_task_by_pr(pg_dsn, "org/repo", 1234) is None
```

(Check the existing file's fixture name for the Postgres DSN — likely `pg_dsn` or built from `settings.database_url`; match its convention and reuse its schema-cleanup pattern.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/integration/test_db.py -v -m integration`
Expected: FAIL — `upsert_task() got an unexpected keyword argument 'pr_number'`

- [ ] **Step 3: Write minimal implementation**

`app/db.py` changes:

- DDL: add `pr_number int` after `pr_url text`, plus in `ensure_schema`:
  `await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS pr_number int")`
- `upsert_task`: add `pr_number=None` keyword; INSERT column list gains `pr_number` with `$10`; ON CONFLICT gains `pr_number = $10`.
- New function:

```python
async def get_task_by_pr(dsn, repo, pr_number):
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """SELECT * FROM tasks
               WHERE repo = $1 AND pr_number = $2
               ORDER BY created_at DESC LIMIT 1""", repo, pr_number)
        if not row:
            return None
        task = dict(row)
        if isinstance(task.get("retry_counts"), str):
            task["retry_counts"] = json.loads(task["retry_counts"])
        return task
    finally:
        await conn.close()
```

`app/services/run_manager.py` `_mirror` — append `pr_number=values.get("pr_number")` to the `upsert_task(...)` call.

`app/graph/state.py` — in `TaskState`, after `pr_url: Optional[str]` add `pr_number: Optional[int]` and add `pr_number=None` to `initial_state`'s returned dict if it constructs explicit fields.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/integration/test_db.py tests/test_run_manager_failures.py -v`
Expected: PASS (run-manager mirror change is exercised by its existing fakes; if `_mirror` tests use `pg_dsn=None` nothing breaks).

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/services/run_manager.py app/graph/state.py tests/integration/test_db.py
git commit -m "feat: tasks table pr_number column and PR-based task lookup"
```

---

### Task 5: human_approval node opens the draft PR

**Files:**
- Modify: `app/graph/nodes/human_approval.py`
- Test: `tests/test_gate_nodes.py` (append; check existing test names in that file first and follow their `make_services` usage)

**Interfaces:**
- Consumes: `ensure_draft_pr` (Task 3), existing interrupt resume payload `{"decision": ..., "feedback": ...}` (unchanged).
- Produces: interrupt payload gains `"pr": {"number", "html_url", "draft"}`; both resume `Command(update=...)` branches gain `"pr_url"` and `"pr_number"` so downstream state/tasks-table mirror them. Pre-interrupt side effects are idempotent (Task 3 guard).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gate_nodes.py` (adapt fixture/naming to that file's existing style):

```python
@pytest.mark.anyio
async def test_human_approval_opens_draft_pr_before_interrupt():
    import pytest as _pytest
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import interrupt
    from tests.fakes import StubGitHub, make_services

    gh = StubGitHub()
    services = make_services(github=gh)

    async def node(state):
        from app.graph.nodes.human_approval import human_approval_node
        return await human_approval_node(state, services=services)

    # Minimal two-node graph: human_approval interrupts; a sink swallows resume.
    import langgraph.graph as lg
    g = lg.StateGraph(dict)
    g.add_node("human_approval", node)
    g.add_node("sink", lambda s: s)
    g.add_edge(lg.START, "human_approval")
    graph = g.compile(checkpointer=InMemorySaver())

    config = {"configurable": {"thread_id": "t-pr-gate"}}
    state = {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
             "task_description": "d", "plan": "p", "code_diff": "x",
             "test_results": None, "review_comments": [], "cost_so_far": 0.0,
             "retry_counts": {}}
    it = None
    async for chunk in graph.astream(state, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            it = chunk["__interrupt__"]
    assert it is not None
    payload = it[0].value
    assert payload["pr"]["number"] == 11
    assert payload["pr"]["html_url"].endswith("/pull/11")
    assert gh.created_prs and gh.created_prs[0]["draft"] is True

    result = None
    async for chunk in graph.astream(
            Command(resume={"decision": "approved", "feedback": ""}), config,
            stream_mode="updates"):
        if "human_approval" in chunk:
            result = chunk["human_approval"]
    assert result["__goto__"] == "commit_pr" or result is not None
    # idempotent: resume re-run must NOT create a second PR
    assert len(gh.created_prs) == 1
```

Note: this test drives `human_approval` in isolation with a sink node; the assertion on `__goto__` can be relaxed to just "node re-ran without creating a second PR" if the graph shape makes goto awkward — the essential assertions are: PR in interrupt payload, and exactly one `created_prs` entry after resume.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gate_nodes.py -v -k pr`
Expected: FAIL — interrupt payload has no `"pr"` key.

- [ ] **Step 3: Write minimal implementation**

Rewrite `app/graph/nodes/human_approval.py`:

```python
from inspect import isawaitable

from langgraph.types import Command, interrupt

from app.graph.nodes.helpers import emit
from app.github.pr_flow import ensure_draft_pr


async def human_approval_node(state, *, services):
    """LangGraph interrupt() gate — pauses until resumed with a decision payload.

    Pre-interrupt side effect: push the branch and open a draft PR. This node
    re-runs from the top on resume, so ensure_draft_pr is idempotent (it returns
    the existing open PR for the head branch instead of creating another one).
    """
    await emit(services, state, "human_approval", "node_started", {})
    pr = await ensure_draft_pr(services, state)
    await emit(services, state, "human_approval", "tool_call",
               {"pr_url": pr["html_url"], "pr_number": pr["number"]})
    payload = interrupt({
        "task_id": state["task_id"],
        "pr": pr,
        "summary": {"plan_chars": len(state.get("plan") or ""),
                    "diff_chars": len(state.get("code_diff") or ""),
                    "test_results": state.get("test_results"),
                    "review_comments": state.get("review_comments"),
                    "cost_so_far": state.get("cost_so_far")},
        "instructions": "Resume with {'decision': 'approved' | 'rejected', 'feedback': '...'}",
    })
    if isawaitable(payload):
        payload = await payload
    decision = payload.get("decision")
    feedback = payload.get("feedback") or ""
    pr_update = {"pr_url": pr["html_url"], "pr_number": pr["number"]}
    if decision == "approved":
        return Command(update={"approval_status": "approved",
                               "status": "committing", **pr_update},
                       goto="commit_pr")
    desc = state["task_description"] + (f"\n\nHUMAN FEEDBACK: {feedback}" if feedback else "")
    return Command(update={"approval_status": "rejected", "task_description": desc,
                           "status": "planning", **pr_update},
                   goto="planner")
```

Important: LangGraph re-execution means `ensure_draft_pr` runs again on resume — the idempotency test above is what guarantees no duplicate PR. Also verify `emit(...)`'s `FakePublisher` tolerates the extra event (it collects everything).

Existing gate tests for approved/rejected routing must still pass unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gate_nodes.py -v`
Expected: PASS including pre-existing routing tests.

- [ ] **Step 5: Commit**

```bash
git add app/graph/nodes/human_approval.py tests/test_gate_nodes.py
git commit -m "feat: open draft PR at the HumanApproval interrupt (idempotent)"
```

---

### Task 6: Webhook signature verification + event classification (pure functions)

**Files:**
- Create: `app/github/webhooks.py`
- Test: `tests/test_webhooks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:

```python
def verify_signature(secret: str, body: bytes, header: str | None) -> bool
    # header format "sha256=<hexdigest>"; constant-time compare; False on any mismatch/absence

def parse_comment(text: str) -> tuple[str, str] | None
    # leading "approve" -> ("approved", ""); leading "reject:" or "reject" -> ("rejected", reason)
    # case-insensitive; anything else -> None

def classify_event(event: str, payload: dict) -> dict | None
    # returns {"pr_number": int, "decision": "approved"|"rejected", "feedback": str} or None
```

Classification rules (spec section 9):
- `pull_request_review` + `action=="submitted"` + `review.state=="approved"` → approved, feedback = review body or "".
- `pull_request_review` + `action=="submitted"` + `review.state=="changes_requested"` → rejected, feedback = review body.
- `pull_request` + `action=="ready_for_review"` → approved.
- `issue_comment` + `action=="created"` + payload has `issue.pull_request` key + commenter is not a bot (`payload["comment"]["user"]["type"] != "Bot"`) → `parse_comment(payload["comment"]["body"])`; None passthrough if unparseable.
- Everything else → None. `pr_number` comes from `payload["pull_request"]["number"]` (for `issue_comment` it's `payload["issue"]["number"]`).

- [ ] **Step 1: Write the failing test**

`tests/test_webhooks.py`:

```python
import hashlib
import hmac as hmac_mod

from app.github.webhooks import classify_event, parse_comment, verify_signature

SECRET = "whsec"
BODY = b'{"action": "submitted"}'


def _sig(body, secret=SECRET):
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_accepts_valid():
    assert verify_signature(SECRET, BODY, _sig(BODY)) is True


def test_verify_signature_rejects_bad_and_missing():
    assert verify_signature(SECRET, BODY, "sha256=" + "0" * 64) is False
    assert verify_signature(SECRET, BODY, None) is False
    assert verify_signature(SECRET, BODY, "sha1=abc") is False
    assert verify_signature("", BODY, _sig(BODY)) is False


def test_parse_comment():
    assert parse_comment("approve") == ("approved", "")
    assert parse_comment("Approve please") == ("approved", "")
    assert parse_comment("reject: the plan missed edge cases") == (
        "rejected", "the plan missed edge cases")
    assert parse_comment("Reject") == ("rejected", "")
    assert parse_comment("LGTM ship it") is None
    assert parse_comment("") is None


def test_classify_review_approved():
    payload = {"action": "submitted",
               "pull_request": {"number": 9},
               "review": {"state": "approved", "body": "nice"}}
    assert classify_event("pull_request_review", payload) == {
        "pr_number": 9, "decision": "approved", "feedback": "nice"}


def test_classify_changes_requested():
    payload = {"action": "submitted",
               "pull_request": {"number": 9},
               "review": {"state": "changes_requested", "body": "redo it"}}
    assert classify_event("pull_request_review", payload) == {
        "pr_number": 9, "decision": "rejected", "feedback": "redo it"}


def test_classify_ready_for_review():
    payload = {"action": "ready_for_review", "pull_request": {"number": 4}}
    assert classify_event("pull_request", payload) == {
        "pr_number": 4, "decision": "approved", "feedback": ""}


def test_classify_comment_command_ignores_bots_and_plain_comments():
    base = {"action": "created",
            "issue": {"number": 9, "pull_request": {"url": "x"}},
            "comment": {"body": "approve", "user": {"type": "User"}}}
    assert classify_event("issue_comment", base) == {
        "pr_number": 9, "decision": "approved", "feedback": ""}
    bot = {**base, "comment": {"body": "approve", "user": {"type": "Bot"}}}
    assert classify_event("issue_comment", bot) is None
    plain = {**base, "comment": {"body": "looks good", "user": {"type": "User"}}}
    assert classify_event("issue_comment", plain) is None
    issue_only = {"action": "created",
                  "issue": {"number": 9}, "comment": {"body": "approve",
                                                      "user": {"type": "User"}}}
    assert classify_event("issue_comment", issue_only) is None


def test_classify_ignores_other_events():
    assert classify_event("push", {"action": "x"}) is None
    assert classify_event("pull_request", {"action": "opened"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.github.webhooks'`

- [ ] **Step 3: Write minimal implementation**

`app/github/webhooks.py`:

```python
import hashlib
import hmac


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_comment(text: str) -> tuple[str, str] | None:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    if lowered.startswith("approve"):
        return "approved", ""
    if lowered.startswith("reject:"):
        return "rejected", stripped[len("reject:"):].strip()
    if lowered.startswith("reject"):
        return "rejected", ""
    return None


def classify_event(event: str, payload: dict) -> dict | None:
    pr_number = None
    if event == "pull_request_review":
        action = payload.get("action")
        review = payload.get("review") or {}
        state = review.get("state")
        if action != "submitted" or state not in ("approved", "changes_requested"):
            return None
        pr_number = (payload.get("pull_request") or {}).get("number")
        decision = "approved" if state == "approved" else "rejected"
        return {"pr_number": pr_number, "decision": decision,
                "feedback": review.get("body") or ""}
    if event == "pull_request":
        if payload.get("action") != "ready_for_review":
            return None
        return {"pr_number": (payload.get("pull_request") or {}).get("number"),
                "decision": "approved", "feedback": ""}
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        if payload.get("action") != "created" or "pull_request" not in issue:
            return None
        comment = payload.get("comment") or {}
        if (comment.get("user") or {}).get("type") == "Bot":
            return None
        parsed = parse_comment(comment.get("body") or "")
        if not parsed:
            return None
        return {"pr_number": issue.get("number"),
                "decision": parsed[0], "feedback": parsed[1]}
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_webhooks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/github/webhooks.py tests/test_webhooks.py
git commit -m "feat: webhook HMAC verification and event classification"
```

---

### Task 7: Webhook route

**Files:**
- Create: `app/api/routes_webhooks.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_webhooks_route.py`

**Interfaces:**
- Consumes: `verify_signature`, `classify_event` (Task 6); `db.get_task_by_pr` (Task 4); `RunManager.resume(task_id, decision, feedback)` raising `AlreadyRunning` / `NotAwaitingApproval` / `TaskNotFound`.
- Produces: `POST /webhooks/github` → 202 on resumed, 200 on ignored/unmatched, 400 if secret unset or signature invalid, 404 task unknown, 409 already running/not awaiting.

- [ ] **Step 1: Write the failing test**

`tests/test_webhooks_route.py` — follow the app-construction pattern from `tests/integration/test_api_flow.py` (read that file first; it builds `create_app(services=..., graph=...)` and uses a TestClient). Key design: the route resolves RunManager via `request.app.state.run_manager`; tests swap it with a fake.

```python
import hashlib
import hmac as hmac_mod
import json

import pytest

from tests.fakes import FakeRedis, make_services

SECRET = "whsec"


def _post(client, event, payload, *, secret=SECRET, task_row=None):
    body = json.dumps(payload).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac_mod.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "x-github-event": event,
        "content-type": "application/json"}
    if task_row:
        import app.api.routes_webhooks as rw
        async def fake_get_task_by_pr(dsn, repo, pr_number):
            return task_row
        rw.get_task_by_pr = fake_get_task_by_pr
    return client.post("/webhooks/github", content=body, headers=headers)


class FakeRunManager:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def resume(self, task_id, decision, feedback=None):
        if self.error:
            raise self.error(task_id)
        self.calls.append((task_id, decision, feedback))


@pytest.fixture
def app(monkeypatch):
    from app.main import create_app
    services = make_services(settings=__import__("app.config", fromlist=["Settings"]).Settings(
        github_pat="p", github_webhook_secret=SECRET), redis=FakeRedis())
    application = create_app(services=services, graph=None)
    application.state.run_manager = FakeRunManager()
    return application


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def test_valid_review_event_resumes_task(client, app, monkeypatch):
    row = {"task_id": "t1", "repo": "org/repo", "status": "awaiting_approval"}
    resp = _post(client, "pull_request_review",
                 {"action": "submitted", "pull_request": {"number": 9},
                  "review": {"state": "approved", "body": ""}}, task_row=row)
    assert resp.status_code == 202
    assert app.state.run_manager.calls == [("t1", "approved", "")]


def test_reject_comment_carries_feedback(client, app):
    row = {"task_id": "t2", "repo": "org/repo", "status": "awaiting_approval"}
    resp = _post(client, "issue_comment",
                 {"action": "created", "issue": {"number": 9, "pull_request": {"u": 1}},
                  "comment": {"body": "reject: bad approach", "user": {"type": "User"}}},
                 task_row=row)
    assert resp.status_code == 202
    assert app.state.run_manager.calls == [("t2", "rejected", "bad approach")]


def test_invalid_signature_is_400(client):
    resp = _post(client, "pull_request_review", {"action": "submitted"},
                 secret="wrong")
    assert resp.status_code == 400
    assert client.app.state.run_manager.calls == []


def test_unknown_pr_returns_404(client, monkeypatch):
    import app.api.routes_webhooks as rw
    async def none_task(dsn, repo, pr_number):
        return None
    monkeypatch.setattr(rw, "get_task_by_pr", none_task)
    resp = _post(client, "pull_request_review",
                 {"action": "submitted", "pull_request": {"number": 99},
                  "review": {"state": "approved", "body": ""}})
    assert resp.status_code == 404


def test_not_awaiting_returns_409(client, app):
    row = {"task_id": "t3", "repo": "org/repo", "status": "done"}
    resp = _post(client, "pull_request_review",
                 {"action": "submitted", "pull_request": {"number": 9},
                  "review": {"state": "approved", "body": ""}}, task_row=row)
    app.state.run_manager = FakeRunManager(error=__import__(
        "app.services.run_manager", fromlist=["NotAwaitingApproval"]).NotAwaitingApproval)
    # re-post with the failing manager after swapping: simplest is to set before
    # posting — adjust per actual wiring (see Step 3 note)


def test_ignored_events_return_200(client):
    resp = _post(client, "push", {"action": "x"})
    assert resp.status_code == 200
    assert client.app.state.run_manager.calls == []
```

(Note while implementing: `test_not_awaiting_returns_409` above sketches intent — write it cleanly as: build app with `FakeRunManager(error=NotAwaitingApproval)` from the start, then assert 409. Follow `test_api_flow.py`'s real construction idioms, including how `create_app` receives settings/redis; that file is the source of truth for the fixture wiring.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webhooks_route.py -v`
Expected: FAIL — `app.api.routes_webhooks` does not exist / 404 on POST.

- [ ] **Step 3: Write minimal implementation**

`app/api/routes_webhooks.py`:

```python
import app.db as db
from app.github.webhooks import classify_event, verify_signature
from app.services.run_manager import AlreadyRunning, NotAwaitingApproval, TaskNotFound

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    settings = request.app.state.services.settings
    body = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    if not verify_signature(settings.github_webhook_secret, body, sig):
        return _err(400, "invalid signature")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _err(400, "invalid json")
    event = request.headers.get("x-github-event", "")
    decision = classify_event(event, payload)
    if not decision:
        return {"ok": True, "resumed": False}
    task = await db.get_task_by_pr(request.app.state.services.pg_dsn,
                                   _repo(payload), decision["pr_number"])
    if not task:
        return _err(404, "no task for PR")
    try:
        await request.app.state.run_manager.resume(
            task["task_id"], decision["decision"], decision["feedback"])
    except TaskNotFound:
        return _err(404, "task not found")
    except AlreadyRunning:
        return _err(409, "task already running")
    except NotAwaitingApproval:
        return _err(409, "task not awaiting approval")
    return {"ok": True, "resumed": True}


def _repo(payload):
    return ((payload.get("repository") or {}).get("full_name")) or ""


def _err(code, detail):
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": False, "detail": detail}, status_code=code)
```

Notes for the implementer:
- Check `app/main.py` for how `routes_tasks` is included and whether `app.state.services` vs individual attributes are exposed; wire `github_webhook` router the same way (`app.main:70` area). If `app.state.services` doesn't exist, read `create_app` and use the same accessor the tasks router uses.
- `repository.full_name` must match the tasks row's `repo` — the webhook test rows set `"repo": "org/repo"`, so include `"repository": {"full_name": "org/repo"}` in webhook payloads sent by tests that expect a task lookup (the ones passing `task_row`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_webhooks_route.py tests/test_webhooks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/routes_webhooks.py app/main.py tests/test_webhooks_route.py
git commit -m "feat: /webhooks/github route resuming tasks from PR events"
```

---

### Task 8: Real commit_pr node

**Files:**
- Modify: `app/graph/nodes/commit_pr.py`
- Test: `tests/test_commit_pr.py` (create; keep any existing tests passing — check `grep commit_pr tests/` first)

**Interfaces:**
- Consumes: `push_branch`/`branch_for` (Task 2), `GitHubClient`-shaped `services.github` (Task 1/3), state fields `pr_url`, `pr_number` (set by Task 5), `cost_so_far`, `retry_counts`, `paused_at`/`resumed_at`.
- Produces: node returns `{"status": "done", "pr_url": <url>}`; emits `node_started`/`node_completed`. Per spec: mark PR ready, or merge when `settings.merge_pr_when_ready`; append final summary comment (cost, retry cycles, pause stats).

- [ ] **Step 1: Write the failing test**

`tests/test_commit_pr.py`:

```python
import pytest

from app.graph.nodes.commit_pr import commit_pr_node
from tests.fakes import StubGitHub, StubSandbox, make_services


def _state():
    return {"task_id": "t1", "repo": "org/repo", "base_branch": "main",
            "pr_url": "https://github.com/org/repo/pull/11", "pr_number": 11,
            "cost_so_far": 1.25, "retry_counts": {"testing": 2, "coding": 1},
            "paused_at": "2026-09-26T01:00:00+00:00",
            "resumed_at": "2026-09-26T09:00:00+00:00",
            "task_description": "d"}


@pytest.mark.anyio
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


@pytest.mark.anyio
async def test_commit_pr_merges_when_configured():
    from app.config import Settings
    gh = StubGitHub()
    services = make_services(
        github=gh, sandbox=StubSandbox(),
        settings=Settings(github_pat="p", merge_pr_when_ready=True))
    await commit_pr_node(_state(), services=services)
    assert gh.merged == [11]


@pytest.mark.anyio
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commit_pr.py -v`
Expected: FAIL — returns `pr_url: None`, no push/comments.

- [ ] **Step 3: Write minimal implementation**

`app/graph/nodes/commit_pr.py`:

```python
from app.github.pr_flow import ensure_draft_pr, render_pr_body
from app.github.push import branch_for, push_branch
from app.graph.nodes.helpers import emit


async def commit_pr_node(state, *, services):
    """Finalize (reachable only from an approved HumanApproval): push, mark
    ready (or merge per config), append the final summary, mark done."""
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
    if services.settings.merge_pr_when_ready:
        await github.merge_pr(repo, pr["number"])
    else:
        await github.mark_ready(repo, pr["number"])
    retries = state.get("retry_counts") or {}
    summary = "\n".join([
        "## Final summary",
        f"- Total cost: ${state.get('cost_so_far', 0.0):.2f}",
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

Note: if `push_branch` raises (PushError), let it propagate — the graph run manager already routes node exceptions to `failed` status + error_log (run_manager crash mirror). Do not swallow.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commit_pr.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/graph/nodes/commit_pr.py tests/test_commit_pr.py
git commit -m "feat: commit_pr node finalizes PR with summary and ready/merge"
```

---

### Task 9: Integration — full webhook round-trip (approve and reject)

**Files:**
- Create: `tests/integration/test_webhook_roundtrip.py`
- Test: same file

**Interfaces:**
- Consumes: everything above; scenario-test helpers from `tests/integration/test_graph_scenarios.py` (`drive_to_interrupt` pattern, `build_graph(services, InMemorySaver())`).
- Produces: end-to-end proof of the phase-2 gate: "draft PR appears with full description; approve and reject both work from GitHub itself; final PR shows cost + retry counts".

- [ ] **Step 1: Write the failing test**

`tests/integration/test_webhook_roundtrip.py` (mark `@pytest.mark.integration`; build services with fakes + real Redis per conftest; compose the graph with `build_graph`):

```python
import json

import pytest

from langgraph.checkpoint.memory import InMemorySaver

from app.graph.build import build_graph
from app.services.run_manager import RunManager
from tests.fakes import StubGitHub, StubLLM, StubSandbox, make_services

SECRET = "whsec"


def _services(github):
    from app.config import Settings
    return make_services(
        llm=StubLLM([
            "1. read repo\n2. change code",   # planner
            "no research needed",              # researcher
            "print('done')",                   # coding agent
            "pytest -q",                       # tester command handling (match StubLLM usage in test_graph_scenarios)
            "approved",                        # reviewer
        ]),
        sandbox=StubSandbox(),
        github=github,
        settings=Settings(github_pat="p", github_webhook_secret=SECRET),
        pg_dsn=None,
    )


@pytest.mark.integration
async def test_approve_via_webhook_reaches_done_with_pr(pg_dsn_unused, redis_client):
    github = StubGitHub()
    services = _services(github)
    graph = build_graph(services, InMemorySaver())
    rm = RunManager(services, graph)

    state = {"task_id": "t-rt", "task_description": "add a feature",
             "repo": "org/repo", "base_branch": "main", "test_command": "pytest -q",
             "model_overrides": {}}
    from app.graph.state import initial_state
    await rm.prepare("t-rt", initial_state("t-rt", "add a feature", "org/repo",
                                           "main", "pytest -q", {}))
    await rm.start("t-rt", initial_state("t-rt", "add a feature", "org/repo",
                                         "main", "pytest -q", {}))
    # wait for interrupt
    import asyncio
    for _ in range(100):
        st = await rm.get_state("t-rt")
        if st and st.get("status") in ("awaiting_approval", "failed"):
            break
        await asyncio.sleep(0.05)
    st = await rm.get_state("t-rt")
    assert st["status"] == "awaiting_approval"
    # draft PR was created with the task description in the body
    assert len(github.created_prs) == 1
    assert "add a feature" in github.created_prs[0]["body"]
    assert github.created_prs[0]["draft"] is True
```

(While implementing: copy the exact LLM-call ordering used in `tests/integration/test_graph_scenarios.py` — check how many StubLLM responses the full graph consumes and script them accordingly; the list above is indicative. For the reject variant, resume with `{"decision": "rejected", "feedback": "wrong"}` and assert the run ends back at `planning`→ loop with feedback in `task_description`. For approve, after resume wait until status `done`, then assert: exactly one created PR, `github.ready == [11]`, one summary comment containing cost/retry text, and state `pr_url` set.)

Additionally add a Redis+Postgres-backed variant of the webhook route test (reuse Task 7's route test but with `pg_dsn` real and `get_task_by_pr` NOT monkeypatched, `RunManager` real) proving PR-number → task resolution against the real DB and real resume. If timebox pressure hits, this real-DB variant is the must-keep part: it exercises `upsert_task(pr_number=...)` mirror + `get_task_by_pr` together.

- [ ] **Step 2: Run test to verify it fails/wires correctly**

Run: `docker compose up -d; python -m pytest tests/integration/test_webhook_roundtrip.py -v -m integration`
Expected: approve test FAILS until any wiring gap surfaces; fix per Step 3.

- [ ] **Step 3: Fix whatever integration surfaces**

Typical integration-only issues: StubLLM response count/order, `_mirror` needing `pr_number` (Task 4), interrupt payload shape. Fix minimally, no scope creep.

- [ ] **Step 4: Run full suite**

Run: `python -m pytest -m integration -v`
Expected: PASS (all pre-existing integration tests + new ones)

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_webhook_roundtrip.py
git commit -m "test: webhook round-trip approve/reject integration scenarios"
```

---

### Task 10: Env/docs update + full verification

**Files:**
- Modify: `.env.example` (add `GITHUB_WEBHOOK_SECRET`, `MERGE_PR_WHEN_READY`)
- Modify: `README.md` (short phase-2 section: webhook setup on the target repo, HMAC secret, approve/reject comment syntax)
- Test: none new

**Interfaces:**
- Consumes: all previous tasks.
- Produces: operator-facing setup docs.

- [ ] **Step 1: Update `.env.example`**

Append:

```
GITHUB_WEBHOOK_SECRET=          # HMAC secret for /webhooks/github (phase 2)
MERGE_PR_WHEN_READY=false       # true = squash-merge instead of marking ready
```

- [ ] **Step 2: Update README**

Add a "Phase 2 — GitHub approval loop" section documenting:
- Repo webhook: `pull_request_review`, `issue_comment`, `pull_request` events → `POST <public-url>/webhooks/github`, `application/json`, secret = `GITHUB_WEBHOOK_SECRET`.
- Human gate commands: comment `approve` or `reject: <reason>` on the draft PR; PR review approval/changes-requested also work.
- Where the draft PR appears (interrupt payload `pr` field, SSE `tool_call` event, tasks table `pr_url`).

- [ ] **Step 3: Full verification**

Run: `python -m pytest -v` then `python -m ruff check app tests` (or `ruff check .` per pyproject).
Expected: all PASS, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add .env.example README.md
git commit -m "docs: phase-2 webhook setup and configuration"
```

---

## Phase-2 gate checklist (from spec §12)

- [ ] Draft PR appears with full description (task, plan, diff, test results, review comments, cost, retries) — Task 3/5
- [ ] Approve works from GitHub itself (review approval, `approve` comment, marked ready) — Task 6/7
- [ ] Reject works from GitHub itself (`reject: <reason>` → back to Planner with feedback) — Task 6/7
- [ ] Final PR shows cost + retry counts — Task 8
- [ ] Resume-latency stat recorded — already built in Phase 1 (`RunManager.resume` emits `sleep_wake`); verify still emitted on webhook-driven resume (Task 9)
- [ ] `task_id ↔ PR number` stored in tasks table — Task 4