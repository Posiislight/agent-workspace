# Demo walkthrough

`demo/sample-repo/` is a tiny Python project with a failing test, handy as a first
target repo.

1. `docker compose up -d`, fill `.env` (see the README), then `python run.py`.
2. Push `demo/sample-repo/` to a new GitHub repo your `GITHUB_PAT` can write to.
3. Open http://localhost:8000 → **New Task**:
   - Description: `Fix the failing test in tests/test_calc.py so add(a, b) returns the sum.`
   - Repository: the repo from step 2, template: Codex → **Start pipeline**.
4. Watch the stages: a new `aw-task-…` agent appears on your Maritime dashboard, then
   Coding → Tester → Reviewer run inside it.
5. At **Approval** a draft PR is already open (use **View PR**) and the VM is asleep.
   Leave it as long as you like; the sleep/wake card shows how long it slept.
6. Click **Approve**: the same VM wakes in about a second, Commit & PR marks the PR
   ready for review, posts a final summary comment, and the VM is deleted.
