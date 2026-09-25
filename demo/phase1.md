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
