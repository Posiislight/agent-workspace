# agent-workspace

Self-hosted multi-agent AI coding workspace: Planner → Researcher → Coding Agent →
Tester → Reviewer → Human Approval → Commit & PR, running on persistent Maritime VMs.

## Status

Implemented today (phase 1 — orchestrator core):

- Full LangGraph pipeline (Planner → Researcher → Coding Agent → Tester → Reviewer)
  with conditional retry routing and bounded retries
- Human Approval `interrupt()` that survives a full process restart, resumed via the API
- FastAPI surface (`/tasks`, SSE live progress, approve/reject, artifacts)
- Postgres checkpointing + tasks index table; Redis event bus (Streams → SSE)
- Maritime micro-VM per task (warm reuse across retries, sleep at approval)

In progress / planned (see `docs/superpowers/`):

- Phase 2 — GitHub approval loop: draft PR at the approval gate, approve/reject via
  GitHub webhooks, Commit & PR finalization
- Phase 3 — cost tracking (per-call LLM cost, VM awake-minutes, audit rows)

## Phase 2 — GitHub approval loop

At the human-approval gate the system pushes an `aw/{task_id}` branch and opens a
**draft PR** containing the task, plan, diff, test results, reviewer comments, running
cost, and retry counts. The task then sleeps until a decision arrives.

Webhook setup on the target repo (Settings → Webhooks):

- Payload URL: `https://<your-host>/webhooks/github`
- Content type: `application/json`
- Secret: the value of `GITHUB_WEBHOOK_SECRET` (HMAC-SHA256 verified)
- Events: Pull requests, Pull request reviews, Issue comments

Deciding from GitHub itself:

- comment `approve` on the PR, or submit an approving review, or mark the PR
  ready for review → task resumes as approved
- comment `reject: <reason>` (or a changes-requested review) → task resumes as
  rejected; the reason is appended to the task description and the plan is revised
  by the Planner

Once approved, the `commit_pr` node pushes the final branch, marks the PR ready
(or squash-merges when `MERGE_PR_WHEN_READY=true`), and appends a final summary
comment with total cost, retry cycles, and pause/resume timestamps.

## Phase 3 — Cost tracking

Cost accrues in real time while a task runs:

- **Per-call LLM cost**: each model call records OpenRouter token usage, priced
  against a price table fetched once at startup from OpenRouter's `/models`
  endpoint. Every update is persisted with `INCRBYFLOAT aw:{id}:cost` and
  broadcast as a `cost_update` SSE event.
- **VM awake-minutes**: each graph drive opens an awake window on the task's VM;
  time spent paused at the approval gate does not accrue. Minutes are stored in
  `aw:{id}:vm_minutes` and multiplied by `VM_COST_PER_HOUR` ($/hour, default 0)
  into `aw:{id}:vm_cost`.
- **Final totals**: written to the tasks table (`vm_cost`, `vm_minutes` columns)
  and into the final PR description and summary comment as an LLM/VM breakdown.

Where to see it live: the task detail page cost chips (refreshed via SSE),
`GET /tasks/{id}` (`llm_cost`, `vm_cost`, `vm_minutes`), and the Redis key
`aw:{id}:cost`.

## Setup

macOS / Linux:

    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]"
    cp .env.example .env       # fill in keys
    docker compose up -d       # postgres :5433, redis :6380
    python -m pytest tests -m "not integration and not e2e"

Windows (PowerShell):

    python -m venv .venv
    .venv\Scripts\pip install -e ".[dev]"
    copy .env.example .env     # fill in keys
    docker compose up -d       # postgres :5433, redis :6380
    python -m pytest tests -m "not integration and not e2e"

`MARITIME_TEMPLATE_ID`: pick from `curl https://api.maritime.sh/api/templates` —
choose a template with git + python3 + network access. Maritime computers (Researcher's
headful browser) additionally require a paid Maritime plan.

## Run

macOS / Linux:

    .venv/bin/python run.py

Windows (PowerShell):

    .venv\Scripts\python run.py