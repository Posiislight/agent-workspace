# agent-workspace

Self-hosted AI coding pipeline. Describe a change in plain English, pick a GitHub
repo, and five stages take it all the way to a real pull request:

    Coding  →  Tester  →  Reviewer  →  Human approval  →  Commit & PR

Every task runs on its own [Maritime](https://maritime.sh) agent VM: created when the
task starts, asleep while it waits for your approval, woken from the same checkpoint
when you decide, and deleted once the task is done.

## How it works

| Stage | What happens |
|---|---|
| **Coding** | A Maritime agent (Codex or DeepSeek harness) explores the cloned repo, plans, edits files and writes tests inside the VM. |
| **Tester** | Mechanical, no LLM: installs dependencies and runs the repo's real test suite (`python -m pytest -q` by default). Failures go back to Coding with the output. |
| **Reviewer** | The same agent reviews the diff and returns `approved` or `needs_changes`. Change requests go back to Coding. |
| **Human approval** | Pushes an `aw/{task_id}` branch, opens a **draft PR**, and pauses. The VM sleeps, so idle time isn't billed. |
| **Commit & PR** | On approval: pushes the final branch, marks the PR ready for review (or squash-merges), and posts a final summary comment. |

Retries are bounded: 3 test failures or 2 rejected reviews stop the task in
`needs_human` instead of looping forever. A rejection at the approval gate (with a
reason) sends the task back to Coding with your feedback.

**Under the hood:** FastAPI + LangGraph. Graph state is checkpointed in Postgres, so a
paused task survives a server restart. Live progress goes through Redis Streams to the
browser over SSE. The React UI shows the stage timeline, plan, diff, test output,
review comments, a sleep/wake card and a replay of past runs.

## Deciding from GitHub (optional)

Besides the Approve / Reject buttons in the UI, you can decide on the PR itself.
Add a webhook on the target repo (Settings → Webhooks):

- Payload URL: `https://<your-host>/webhooks/github`, content type `application/json`
- Secret: the value of `GITHUB_WEBHOOK_SECRET` (HMAC-SHA256 verified)
- Events: Pull requests, Pull request reviews, Issue comments

Then comment `approve` (or approve the review, or mark the PR ready) to approve, or
`reject: <reason>` (or request changes) to send it back to Coding.

## Setup

Requirements: Python 3.12+, Docker, a Maritime account, and a GitHub token.

    python -m venv .venv
    .venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip ...
    cp .env.example .env                     # then fill in the keys below
    docker compose up -d                     # Postgres :5433, Redis :6380

| Variable | Needed | Notes |
|---|---|---|
| `MARITIME_API_KEY` | yes | Runs the agent VMs. The agent's model usage is billed by Maritime. |
| `GITHUB_PAT` | yes | Fine-grained token with **Contents: Read and write** and **Pull requests: Read and write** on the target repos. |
| `MARITIME_TEMPLATE_ID` | no | Default harness template; the UI lets you pick `codex` or `dsh` per task. |
| `GITHUB_WEBHOOK_SECRET` | no | Only for deciding from GitHub. |
| `MERGE_PR_WHEN_READY` | no | `true` squash-merges on approval instead of marking the PR ready. |
| `VM_COST_PER_HOUR` | no | $/hour used to price VM awake time in the cost summary (default 0). |

## Run

    .venv/bin/python run.py                  # Windows: .venv\Scripts\python run.py

Open http://localhost:8000, click **New Task**, describe the change, choose a repo and
template, and start the pipeline. Approve from the task page when it pauses.

The API is the same surface the UI uses:

| Endpoint | Purpose |
|---|---|
| `POST /tasks` | Start a task (`task_description`, `repo`, `template_id`; optional `base_branch`, `test_command`) |
| `GET /tasks`, `GET /tasks/{id}` | List tasks / full task state |
| `GET /tasks/{id}/events` | Live SSE stream (`/events/history` for the backlog) |
| `POST /tasks/{id}/approve`, `/reject` | Decide at the approval gate (optional `feedback`) |
| `POST /tasks/{id}/restart` | Re-drive a `failed` / `needs_human` task from its last checkpoint |
| `GET /templates`, `GET /github/repos` | Harness templates and repos the token can see |

To tidy the task list without deleting anything: `python scripts/hide_tasks.py --help`.

## Tests

    python -m pytest tests -m "not integration and not e2e"   # fast unit tests
    python -m pytest tests -m integration                     # needs docker compose up

## Layout

    app/api/        FastAPI routes (tasks, GitHub webhooks, templates/repos)
    app/graph/      LangGraph pipeline: nodes, routing, state
    app/sandbox/    Maritime integration: agent VMs, exec, long-running commands
    app/github/     PR lifecycle: push, draft PR, ready/merge, comments
    app/services/   Run manager (drive/pause/resume/restart) and cost tracking
    app/frontend/   React UI (built output in dist/ is served by the API)
    docs/           Design specs and implementation plans
