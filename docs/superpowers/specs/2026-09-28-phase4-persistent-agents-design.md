# agent-workspace — Phase 4/5: Persistent Repo Agents, Follow-ups, Visual Proof, Best-of-N

**Date:** 2026-09-28
**Status:** Approved design, implemented on `claude/intelligent-planck-ta5dpn`

## 1. Purpose

Phase 1–2 gave every task a throwaway-ish Maritime VM that lives for one task. Phase 4/5
turns the VM into the product's headline: **a persistent, sleeping, per-repo agent** that
is woken for each task, each follow-up message, each CI failure, and each Best-of-N
candidate — and costs nothing while it sleeps.

Features, in build order:

1. **Lease scheduler** — respects the Maritime plan limits.
2. **Harness layer** — the coding step can run our OpenRouter loop, Maritime's `dsh`
   template, or a `codex` template.
3. **Repo agent that remembers** — one sleeping VM per `(repo, template, slot)`; warm deps,
   a memory file, and a measured cold-vs-warm startup.
4. **Follow-up turns** — chat box, CI self-heal, and `/aw …` PR comments all resume the
   same task thread on the same branch.
5. **Visual proof** — before/after screenshots from the Maritime computer, attached to the PR.
6. **Best-of-N** — N candidate harnesses race (2 at a time), a judge picks the winner,
   live scoreboard.
7. **Budget cap** — live LLM + VM-minute meter; the task pauses and asks before overspending.

Out of scope (explicitly considered and dropped): fan-out/integrator (needs ≥3 concurrent
VMs to be meaningful), rewind/time-travel (LangGraph can rewind state but not the VM
filesystem; would need per-checkpoint git snapshots — a later phase).

## 2. Plan constraints (Maritime)

| Resource | Limit | Consequence |
|---|---|---|
| Awake VMs (`/api/agents`) | 2 concurrent | Scheduler hands out 2 VM leases |
| Sleeping VMs | unlimited, ~free | Keep one VM per repo/template/slot forever |
| Running computers | 1 concurrent | Researcher and Visual Proof share 1 computer lease |
| Computers vs VM slots | independent | A computer never consumes a VM lease |

## 3. Maritime API facts this design relies on

From the official SDK (`maritime-sh/maritime-sdk`) and the dsh framework guide:

- `POST /api/agents {name, templateId, externalId?, initialEnvVars?}`; `GET /api/agents?externalId=`
  — get-or-create by our own id (`provision`).
- `POST /api/agents/{id}/sleep|start`; sleeping agents auto-wake on the next call.
- `POST /api/agents/{id}/chat {message, conversation_id}` exists, but for dsh each chat
  message is a **one-shot headless run** that returns only the final text.
- The dsh template: Node 22, pinned dsh, Workbench on :3080, state in `/data/.dsh`,
  `DSH_HEADLESS=true`, `DSH_MODEL` env.
- `/exec` and `/files/*` (used since phase 1) are not in the public SDK — undocumented but
  working; we keep depending on them.

**Decision:** harnesses are driven through `exec`/`run_long` inside `/data/workspace`, not
`/chat`: we need exit codes, logs, >120s runs, and the diff in our own workspace so the
Tester / push / PR code is harness-agnostic. Conversation continuity comes from the
LangGraph thread + the repo on disk, not from `conversation_id`.

## 4. Lease scheduler (`app/services/leases.py`)

`LeasePool(max_vms=2, max_computers=1)`, in-process (single API process, like phase 1).

- `acquire_vm(agent_key, owner, on_wait)` — waits until the agent is not owned by another
  task **and** an awake slot is free (an agent already awake keeps its slot). Re-entrant for
  the same owner. `on_wait` fires once so the UI can show `queued for VM`.
- `release_vm(agent_key)` — called from `sleep()`.
- `acquire_computer(owner)` / `release_computer(owner)` — same, 1 slot.

Ownership doubles as the **per-repo mutex**: two tasks on the same repo never interleave
in one workspace. A task owns its agent from first `ensure()` until the drive pauses or ends
— `RunManager.drive` now always sleeps the workspace in `finally`, which releases the lease.

## 5. Harness layer (`app/harness/`)

```
harness name → (template id, how the coding step runs)
  openrouter → MARITIME_TEMPLATE_ID, today's JSON-ops loop (unchanged behaviour)
  dsh        → MARITIME_TEMPLATE_DSH (default "dsh"),   run_long HARNESS_CMD_DSH
  codex      → MARITIME_TEMPLATE_CODEX (default "codex"), run_long HARNESS_CMD_CODEX
```

CLI harnesses: write the brief to `/data/.runs/brief-{ts}.md`, run the configured command
(`{brief}` placeholder) with `run_long`, then read `diff()`. Empty diff or non-zero exit →
same `needs_human`/retry semantics as the OpenRouter loop. Model keys for the harness are
seeded at agent creation from `HARNESS_ENV` (JSON object → `initialEnvVars`, secret).
Template ids and commands are config because `GET /api/templates` is the source of truth.

## 6. Repo agent that remembers (`app/sandbox/workspace.py`)

- `RepoAgent` = one Maritime VM, `externalId = aw-repo:{owner/repo}:{template}:{slot}`,
  found via `GET /api/agents?externalId=` before creating (survives our process restarts).
- `TaskWorkspace` = a task's view onto a `RepoAgent`. `ensure()`:
  1. lease (4); 2. agent get-or-create + provision (clone once); 3. **activate** the task:
     `git fetch`, commit any WIP of the previous task onto its own branch, then
     `git checkout aw/{task_id}` (existing) or `git checkout -B aw/{task_id} origin/{base}`,
     and refresh the local base ref so `diff()` stays against the fresh base.
  4. deps: `pip install` only when `sha256(requirements.txt)` differs from
     `/data/.aw/deps.sha`.
- Memory: `/data/.aw/memory.md` — Planner reads it into its prompt; `commit_pr` and the
  follow-up path append a one-line record (task, files touched, test command, outcome).
- Metrics: every `ensure()` that activates records `{cold, clone, deps_cached,
  ready_seconds}` into `state.workspace_metrics` and a `workspace_ready` event. The UI and PR
  body show cold vs warm — the before/after measurement.
- VM cost: `TaskWorkspace` accrues awake seconds while owned; nodes fold them into
  `state.vm_seconds` / `vm_cost` at `VM_COST_PER_MINUTE` (nominal, configurable).

## 7. Follow-up turns

One primitive: `RunManager.followup(task_id, instruction, source)`.

| Task status | Mechanism |
|---|---|
| `awaiting_approval` / `awaiting_budget` | resume the interrupt with `{"decision": "followup", "feedback": instruction}` |
| `done`, `needs_human` | new input on the finished thread; `START` routes to `coding_agent` when `followup_request` is set |
| running | 409 (`AlreadyRunning`) |
| `done` with `MERGE_PR_WHEN_READY=true` | 409 — the branch is merged |

Path: `coding_agent` (brief contains the follow-up request, retry counters reset) → Tester →
Reviewer → Visual Proof → Human Approval (pushes the branch, updates the PR body, pauses).
`state.followups` keeps the chat history `{source, instruction, at}`.

Triggers:

- **Chat:** `POST /tasks/{id}/followups {message}`.
- **PR comment:** an `issue_comment` or `pull_request_review_comment` starting with `/aw `.
- **CI self-heal:** `check_run` with `action=completed` and conclusion in
  `failure|timed_out` on a branch `aw/{task_id}` → fetch the job log tail
  (`GET /repos/{repo}/actions/jobs/{id}/logs`), dedupe on `(head_sha, check name)`, cap at
  `CI_AUTOFIX_MAX_ATTEMPTS` (default 2) per task.

## 8. Visual proof

Node `visual_proof` between Reviewer(approved) and Human Approval. Runs only when the task
has a preview config (`preview_command`, `preview_port`, `preview_path`) — from the task
request or `.aw.json` in the repo. Steps, under the computer lease:

1. push `aw/{task_id}`;
2. on the computer: clone once to `~/aw-proof/{repo}`; for `base` then `aw/{task_id}`:
   checkout, optional setup (venv + requirements), start the app with `nohup`, wait for the
   port, open Chromium, screenshot, kill the app;
3. store PNGs in Redis (`aw:{task}:proof:{before|after}`) served by
   `GET /tasks/{id}/proof/{name}.png`, and commit them to the `aw-artifacts` branch via the
   GitHub Contents API so the PR body can embed them;
4. sleep the computer, release the lease.

Any failure is recorded in `error_log` and skipped — proof never blocks the PR.

## 9. Best-of-N

Task request `candidates: ["codex", "codex", "dsh"]`. Graph: Researcher → `best_of_n`
(instead of `coding_agent`) when `len(candidates) > 1`.

- The main workspace sleeps first (frees its lease).
- Each candidate `i` runs in `TaskWorkspace(repo, template(harness_i), slot=k)` where `k`
  is the occurrence index of that harness, concurrently via `asyncio.gather`; the lease pool
  admits 2 at a time, the third starts when one sleeps.
- Per candidate: harness run → test command → diff + `--shortstat` → sleep. Scoreboard row:
  `{index, harness, status, tests_passed, files, insertions, deletions, seconds, vm_seconds,
  score, winner}` emitted as `candidate_update` events and kept in `state.candidates`.
- Judge: reviewer model gets the passing candidates' diffs and returns
  `{"winner": i, "scores": {...}, "rationale": "..."}`. Fallback: first passing, then fewest
  changed lines. No candidate passes → `needs_human`.
- The winner's slot becomes `state.workspace_slot` / `state.harness`, its test run becomes
  `state.test_results`, and the graph continues at the Reviewer (the winner already passed
  the suite in its own workspace) → Visual Proof → … as usual. Later retries use the
  winner's harness and workspace.

## 10. Budget cap

- `budget_usd` on the task (optional; `DEFAULT_BUDGET_USD`, 0 = none).
- `cost_so_far = llm_cost + vm_cost`; every `cost_update` event carries the breakdown.
- `check_budget` at the top of Coding, Tester, Best-of-N and Visual Proof: over budget →
  `interrupt({"kind": "budget", ...})`; the drive sleeps the VM and mirrors
  `status = awaiting_budget`.
- `POST /tasks/{id}/budget {budget_usd}` resumes with a raised limit;
  `POST /tasks/{id}/reject` (or budget `0` stop) ends the task as `needs_human`.

## 11. API additions

| Endpoint | Purpose |
|---|---|
| `POST /tasks` | + `harness`, `candidates`, `budget_usd`, `preview_command`, `preview_port`, `preview_path` |
| `POST /tasks/{id}/followups` | chat follow-up |
| `POST /tasks/{id}/budget` | raise budget and continue |
| `GET /tasks/{id}/proof/{name}.png` | visual proof images |
| `GET /workspaces` | lease pool + known repo agents (awake/sleeping, owner) |
| `POST /webhooks/github` | + `check_run`, `/aw` comments |

## 12. Configuration

```
MAX_AWAKE_VMS=2  MAX_RUNNING_COMPUTERS=1
MARITIME_TEMPLATE_DSH=dsh  MARITIME_TEMPLATE_CODEX=codex
HARNESS_CMD_DSH='dsh --profile headless "$(cat {brief})"'
HARNESS_CMD_CODEX='codex exec --full-auto --skip-git-repo-check "$(cat {brief})"'
HARNESS_ENV='{"OPENAI_API_KEY": "...", "DEEPSEEK_API_KEY": "..."}'
VM_COST_PER_MINUTE=0.002  DEFAULT_BUDGET_USD=0
CI_AUTOFIX_MAX_ATTEMPTS=2  PROOF_ARTIFACTS_BRANCH=aw-artifacts
```

## 13. Testing

Unit tests with the existing fakes: lease pool fairness/limits/re-entrancy, workspace
activation scripts and deps-cache, CLI harness, follow-up routing (interrupt and finished
thread), webhook classification for `check_run` and `/aw`, visual proof with stub computer,
Best-of-N with 3 candidates under a 2-slot pool (asserts max concurrency 2), budget
interrupt/resume.

## 14. Risks

- Template ids `dsh`/`codex` are unverified from here (maritime.sh is not reachable from the
  build sandbox) — both are config; verify with `GET /api/templates`.
- Non-Python templates may lack `python3-venv`; provisioning tolerates a missing venv and
  the Tester then uses the system interpreter.
- The lease pool is in-process; running several API replicas needs a Redis-backed pool.
