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