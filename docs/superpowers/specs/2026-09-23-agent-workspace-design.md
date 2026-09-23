# agent-workspace — Multi-Agent AI Coding Workspace

**Date:** 2026-09-23
**Status:** Approved design (pending implementation plan)

## 1. Purpose

A self-hosted, multi-agent AI coding workspace. Given an engineering task, the system
coordinates the full workflow end-to-end:

```
Planner → Researcher → Coding Agent → Tester → Reviewer → Human Approval → Commit & PR
```

It is infrastructure, not "an LLM that writes code": every node's output is inspectable,
every run is resumable after a crash or a paused approval, code execution happens in an
isolated Maritime micro-VM, and every dollar spent is tracked back to the task that spent it.

The system also serves as a public showcase of Maritime: one persistent VM per task that
stays alive through planning, research, coding, and testing; a headful-browser Researcher
that is watchable live; and a sleep/wake story with a measured overnight-resume statistic.

### Decisions made during design

| Decision | Choice |
|---|---|
| Location | Standalone repo at `C:\Users\adele\agent-workspace` (no relation to digenty) |
| Project name | `agent-workspace`; agents named `aw-task-{task_id}` |
| Sandbox | Maritime directly (no fallback abstraction) |
| LLM provider | OpenRouter / multi-model; per-role model selection |
| GitHub auth | Fine-grained PAT first; GitHub App later |
| First target repos | Small test repo (Python) to keep the loop fast; Java repos supported later |
| Build approach | Phased: (1) orchestrator core, (2) GitHub approval loop, (3) cost tracking & hardening |

## 2. Tech stack

| Layer | Choice |
|---|---|
| Orchestration | LangGraph (Python 3.12+) with `langgraph-checkpoint-postgres` |
| API | FastAPI (async), uvicorn |
| Job state / events | Redis (Streams for the event bus, keys for state/cost) |
| Sandbox | Maritime micro-VMs via its REST API (`https://api.maritime.sh`, `mk_` Bearer key) |
| Headful browser | Maritime "computers" REST API (same key, `computers` scope required) |
| Source control | GitHub over HTTPS with a fine-grained PAT (App integration deferred) |
| Persistence | Postgres — LangGraph checkpoints + a small `tasks` index table |
| Streaming | Server-Sent Events, one stream per `task_id` |

## 3. Repo layout

```
agent-workspace/
├── docker-compose.yml          # Redis 7 + Postgres 16 for local dev
├── app/
│   ├── main.py                 # FastAPI entrypoint
│   ├── api/                    # routes: tasks, events (SSE), approval, webhooks
│   ├── graph/
│   │   ├── state.py            # TaskState
│   │   ├── nodes/              # planner, researcher, coding_agent, tester, reviewer, commit_pr
│   │   └── edges.py            # conditional routing + retry-bound logic
│   ├── llm/                    # OpenRouter client; usage extraction; price table cache
│   ├── sandbox/                # Maritime wrapper (VM + computers)
│   ├── github/                 # PAT-based PR client + webhook receiver
│   ├── events/                 # Redis Streams publisher, SSE bridge
│   ├── cost/                   # cost accumulator
│   └── config.py               # pydantic-settings, all config from env vars
├── tests/
├── demo/                       # demo script: overnight pause + resume timing
└── pyproject.toml
```

## 4. TaskState

Single source of truth; checkpointed by LangGraph on every super-step.

```python
class TaskState(TypedDict):
    task_id: str
    task_description: str
    repo: str                      # e.g. "org/repo"
    base_branch: str
    plan: Optional[str]
    research_notes: Optional[str]
    code_diff: Optional[str]
    test_results: Optional[TestResult]
    review_comments: Optional[list[str]]
    approval_status: Literal["pending", "approved", "rejected", "needs_changes"]
    retry_counts: dict[str, int]   # "testing" and "coding" counters
    cost_so_far: float             # USD
    sandbox_id: Optional[str]      # Maritime agent id (VM), reused across retries
    status: Literal["planning", "researching", "coding", "testing", "reviewing",
                    "awaiting_approval", "committing", "done", "failed", "needs_human"]
    error_log: list[str]
    model_overrides: dict[str, str]   # per-role model ids, from submission
    paused_at: Optional[str]       # ISO ts when entering awaiting_approval (demo stat)
```

Deliberate extensions to the original prompt schema (accepted in review):
- `"needs_human"` status — distinguishes a bound-exceeded task from a crashed one.
- `model_overrides`, `paused_at` — carry per-task config and the demo statistic.

`TestResult` is a structured object: pass/fail, failing test names, exact error output
(handed verbatim to the Coding Agent on retry).

## 5. Graph definition

```
Planner → Researcher → CodingAgent → Tester
   ↑                                    │
   │                     tests fail ────┤ tests pass
   │                        │           │
   │              retry_counts["testing"]++     │
   │              (bound 3; exceeded → needs_human)
   │                        ↓           ↓
   │                   CodingAgent   Reviewer
   │                                     │
   │                    review rejected ─┤ review approved
   │              retry_counts["coding"]++
   │              (bound 2; exceeded → needs_human)
   │                        ↓           ↓
   │                   CodingAgent   HumanApproval ← interrupt()
   │                                             │
   │                              ┌──────────────┴──────────────┐
   └── rejected/needs_changes ────┘                       approved
       (human feedback appended to task_description)         ↓
                                                        CommitAndPR
```

Rules:
- **Human rejection** loops back to Planner with the feedback appended to
  `task_description` — the plan is revised, not the code.
- **CommitAndPR is reachable only from an approved HumanApproval.** No other edge leads to it.
- **Bound exceeded → `needs_human`** terminal state with full context preserved
  (state snapshot, plan, diff, test output, review comments, error log). Never silently dropped.

### Retry semantics (resolving the prompt's ambiguity)

Both test-failure and review-rejection re-entries go into CodingAgent, so they are counted
separately:
- `retry_counts["testing"]` — incremented on each test-failure re-entry; bound **3**.
- `retry_counts["coding"]` — incremented on each review-rejection re-entry; bound **2**.

On every re-entry, the Coding Agent's prompt includes the exact failure output / review
comments, instructing it to address those specifically rather than rewriting unrelated code.

### Transient vs semantic failures

- **Transient** (HTTP 429/5xx, timeouts, rate limits — LLM or Maritime): retried inside the
  node with exponential backoff (1s → 4s → 16s, 3 attempts). Never touches `retry_counts`.
- **Semantic** (failing tests, review rejection): counted against bounds as above.
- **Unexpected crash**: recorded in `error_log`, status → `failed`; the checkpointer allows
  a manual re-run from the last checkpoint.

### Checkpointing

`AsyncPostgresSaver` with `thread_id = task_id`; state persists on every edge transition.
The HumanApproval `interrupt()` is therefore safe to leave open for days and survives a
full process restart (acceptance criterion: kill process → restart → resume from the same point).

## 6. Sub-agents

All five reasoning agents are LangGraph nodes calling OpenRouter; Maritime provides the
shared workspace. Each role maps to a model id from config (defaults: Planner/Reviewer/
Tester/Researcher on a fast cheap model, e.g. `openai/gpt-4.1-mini`; Coding Agent on a
strong model, e.g. `anthropic/claude-sonnet-4.5`), overridable per task at submission.
System prompts follow the build prompt verbatim (Planner: read-only, states interpretation,
revises rather than drops feedback; Researcher: cites sources, no implementation code;
Coding Agent: smallest correct change, address retry feedback specifically, never pushes;
Tester: never fixes, reports structured failures; Reviewer: gates before human review,
"when in doubt, approve").

| Agent | Reads | Writes | Tools |
|---|---|---|---|
| Planner | task_description, repo (read-only, via VM) | plan | VM files/list, files/download, read-only exec |
| Researcher | plan, repo | research_notes | web + VM read + Maritime computer (headful browser) |
| Coding Agent | plan, research_notes, sandbox_id, retry context | code_diff, sandbox_id | VM read/write/exec, git in VM |
| Tester | sandbox_id, code_diff | test_results | VM exec via run_long |
| Reviewer | plan, code_diff, test_results | review_comments, approval_status | none (diff + test output only) |

## 7. Maritime integration

### VM lifecycle — one VM per task, shared by all repo-touching agents

- Created lazily on first Planner entry (Planner is the first node that needs the repo).
- Name: `aw-task-{task_id}` — findable in Maritime's dashboard for the demo.
- Provisioning (via `exec`): clone repo at `base_branch` into `/data/workspace`; install
  dependencies into a venv on `/data`. Both survive sleep, restart, and redeploy.
- **Re-entry always reuses** the existing VM (`GET /api/agents/{sandbox_id}` → wake comes
  free with any call). If it 404s (deleted/crashed), recreate + re-provision and record the
  incident in `error_log`.
- Entering `awaiting_approval` → explicit `POST .../sleep` (idle-sleep billing keeps the
  pause cheap).
- VM restart/redeploy keeps `/data`; a `DELETE` removes it — so warm re-runs hold for
  restart/redeploy/sleep-wake, which is the honest reading of the "kill mid-task" criterion.

### Beating the 120s `exec` cap

Maritime `exec` is one-shot, timeout ≤ 120s, output ≤ 256KB. Everything long-running goes
through one pattern:

`Sandbox.run_long(cmd)` → launch detached (`nohup ... > /data/.runs/{run_id}.log 2>&1 & echo $!`)
→ poll with short execs (`kill -0 <pid>` + `tail` the log) until the process exits or a
global run timeout (configurable, default 30 min) is hit. Short commands use plain `exec`.

### Researcher's headful browser — first-class, not optional

- A Maritime "computer" (persistent headful desktop) is created on first Researcher entry.
- Driven via the computers REST API (`POST /api/v1/computers/{id}/actions`, wake/sleep).
- Every step emits a `tool_call` SSE event carrying a screenshot
  (`GET /api/v1/computers/{id}/screenshot`); `POST .../viewer` yields a live watchable /
  recordable session — the screen-record material for the demo.
- The computer sleeps/wakes with the task like the VM.
- Config: the Maritime API key must carry the `computers` scope.

### Sleep/wake as a measured headline

- Entering `awaiting_approval` sets `paused_at` and emits it.
- The first event after resume emits `resumed_at` plus the measured resume latency as a
  dedicated `sleep_wake` event. The number is produced by the system, not hand-timed.
- `demo/` contains the scripted overnight-pause demo: submit → sleep at approval → approve
  next morning → report "paused Xh Ym, resumed in Zs".

## 8. API, events & persistence

### FastAPI endpoints (phase 1)

| Route | Purpose |
|---|---|
| `POST /tasks` | body: `task_description`, `repo`, `base_branch`, optional `model_overrides` → creates TaskState, starts the graph run, returns `task_id` immediately |
| `GET /tasks/{id}` | current TaskState snapshot (Redis first, Postgres fallback) |
| `GET /tasks/{id}/events` | SSE stream over the task's Redis Stream |
| `POST /tasks/{id}/approve` · `reject` | resume the interrupted graph (reject takes optional feedback text) |
| `GET /tasks/{id}/artifacts` | plan, research notes, diff, test results |
| `POST /webhooks/github` | (phase 2) PR review/comment events → resume path |

### Redis schema (`aw:{task_id}:*`)

- `state` — JSON mirror of TaskState for fast reads.
- `events` — Stream; SSE subscribes. Every node publishes on start/output/finish:
  `{"task_id","node","type","data","ts"}` with types `node_started | output_chunk |
  tool_call | node_completed | cost_update | sleep_wake`.
- `cost` — running USD total via `INCRBYFLOAT` (phase 3).
- Idempotency: every job carries dedup key `task_id + node + attempt`.

### Postgres

- LangGraph checkpointer tables (`AsyncPostgresSaver`), `thread_id = task_id`.
- `tasks` index table: task_id, repo, status, timestamps (incl. paused/resumed), final PR
  URL, cost total, retry counts. Powers listing/history and the phase-3 audit rows.

### Process model

One process for now: FastAPI serves; the graph runs as asyncio background work in the same
app. The seam to split API from worker (Redis queue) is documented but not built; nothing
else depends on it.

## 9. Phase 2 — GitHub approval loop

- At the `HumanApproval` interrupt: push the branch, open a **draft PR** (not marked ready)
  with description = task description, plan summary, diff, test results, review comments,
  running cost, and paused/resume stats. Store `task_id ↔ PR number` in the tasks table.
- Repo-level webhooks (`pull_request_review`, `issue_comment`) point at
  `POST /webhooks/github`; HMAC-secret validated. This works with a PAT — no GitHub App yet.
- **Approve** = review approval / `approve` comment / PR marked ready → resume as approved.
- **Reject** = `reject: <reason>` comment → resume as rejected, reason appended to
  `task_description` → back to Planner.
- **Commit & PR agent** (only from an approved gate): ensure branch pushed, mark PR ready
  (or merge per repo config), append the final summary: total cost, agent-minutes, retry
  cycles. Writes status `done` and the PR URL.

## 10. Phase 3 — Cost tracking

- LLM: OpenRouter returns token usage per call; multiply by a per-model price table
  (fetched from OpenRouter's models endpoint, cached) → `INCRBYFLOAT aw:{id}:cost` +
  `cost_update` SSE event per call.
- VM/computer time: tracked as awake-minutes at a configurable nominal rate (Maritime bills
  flat; this is a metric, not a bill).
- Final totals → Postgres audit row + final PR description. Every dollar traceable to its task.

## 11. Configuration

All via env vars (`pydantic-settings`):

```
OPENROUTER_API_KEY          # LLM calls
MARITIME_API_KEY            # mk_ key, computers scope included
GITHUB_PAT                  # fine-grained; contents + PRs read/write on target repos
GITHUB_WEBHOOK_SECRET       # HMAC validation (phase 2)
DATABASE_URL, REDIS_URL
# per-role model ids (e.g. MODEL_PLANNER, MODEL_CODING_AGENT, ...) with sane defaults
SANDBOX_RUN_TIMEOUT_SECONDS # default 1800
```

## 12. Testing strategy

1. **Unit** — graph routing + retry bounds (pure functions over TaskState), event
   publishing, cost math, comment parsing.
2. **Integration** — real Postgres + Redis via docker-compose; stub OpenRouter (canned
   responses) and a fake sandbox implementing the same wrapper interface → the full graph
   runs deterministically, including kill-process-mid-approval → restart → resume.
3. **E2E (real)** — small Python test repo, real Maritime VM, real OpenRouter: full loop,
   a deliberately failing test exercising the retry path, the overnight-pause demo, and the
   webhook round-trip (phase 2).
4. **Acceptance checklist** — see phase gates below.

### Phase gates

- **Phase 1 done when:** submit → SSE shows live node-by-node progress; Tester failure
  auto-routes back with failure details up to bound; interrupt at approval survives a full
  process restart; approve via API completes the run; Maritime VM is reused warm across retries.
- **Phase 2 done when:** draft PR appears with full description; approve and reject both
  work from GitHub itself; final PR shows cost + retry counts; resume-latency stat recorded.
- **Phase 3 done when:** cost visible live and in the final PR/audit row; every acceptance
  criterion from the build prompt checked green.

## 13. Out of scope (explicit)

- GitHub App registration (deferred; PAT covers everything until then).
- Multi-user auth on the API (single-operator tool).
- Frontend dashboard (SSE + curl + Maritime dashboard are the surfaces).
- Parallel task scheduling beyond simple asyncio concurrency.
