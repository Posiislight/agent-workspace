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

    .venv\Scripts\python run.py
