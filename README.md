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

Phase 2 — GitHub approval loop (below).

Phase 4/5 — persistent repo agents (spec:
`docs/superpowers/specs/2026-09-28-phase4-persistent-agents-design.md`):

- **Lease scheduler** — at most 2 awake VMs and 1 running Maritime computer (plan
  limits); sleeping VMs are free, waiting tasks show `queued_for_vm`
- **Repo agent that remembers** — one sleeping VM per `(repo, template, slot)`, reused
  across tasks: warm deps (cached by `requirements.txt` hash), a memory file the Planner
  reads, and cold-vs-warm startup measured on every task (`workspace_ready` events)
- **Harness layer** — `openrouter` (built-in loop), `dsh` and `codex` Maritime templates
  run headless in the workspace
- **Keep chatting after the PR** — chat box / `POST /tasks/{id}/followups`, `/aw …` PR
  comments, and failed CI checks wake the same agent on the same branch
- **Visual proof** — before/after screenshots of the running app from the Maritime
  computer, embedded in the PR
- **Best-of-N** — up to 4 candidates race (2 awake at a time), a judge picks the winner,
  live scoreboard
- **Budget cap** — live LLM + VM-minute meter; the task sleeps and asks before overspending

Not built (considered and dropped): fan-out/integrator, rewind/time-travel.

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

## Phase 4/5 — persistent agents

Task options (`POST /tasks`): `harness` (`openrouter` | `dsh` | `codex`), `candidates`
(e.g. `["codex", "codex", "dsh"]` for Best-of-3), `budget_usd`, and `preview_command` /
`preview_port` / `preview_path` for visual proof. Visual proof can also come from the repo:

    // .aw.json
    {"preview": {"command": "python app.py", "port": 8000, "path": "/"}}

Follow-ups: type in the task page's chat box, or comment `/aw make it async` on the PR.
Add **Check runs** and **Pull request review comments** to the webhook events to enable
CI self-heal (capped by `CI_AUTOFIX_MAX_ATTEMPTS`) and inline `/aw` review comments.

Harness templates: verify ids with `curl https://api.maritime.sh/api/templates` and set
`MARITIME_TEMPLATE_DSH` / `MARITIME_TEMPLATE_CODEX`; the CLI invocations are
`HARNESS_CMD_DSH` / `HARNESS_CMD_CODEX` (`{brief}` = path of the task brief in the VM), and
`HARNESS_ENV` (JSON) seeds the harness VMs' model keys. `GET /workspaces` shows the lease
pool and which repo agents are awake.

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