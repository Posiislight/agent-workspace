# Maritime-Harness Pipeline (v2) — Design

Date: 2026-09-27
Status: Approved (pending spec review)

## 1. Summary

Replace the orchestrator's direct OpenRouter LLM calls with Maritime agent
harnesses. One Maritime agent is created per task, built from a user-selected
template (`codex` or `dsh`). Every LLM stage (Planner, Researcher, Coding,
Tester, Reviewer) is executed by chatting with that agent via Maritime's
`POST /api/agents/{id}/chat` endpoint, which resolves the model through
Maritime's own LLM proxy. OpenRouter, per-stage model overrides, and the
per-stage model selectors on the Create page are removed.

## 2. Verified API facts (probed live 2026-09-27)

- `POST /api/agents {name, templateId}` → 201 with `{id, status: "deploying"}`.
- `GET /api/agents/{id}` → `{status}`; becomes `active` after provisioning
  (observed ~30-60s). Chat before `active` returns
  `{"response": "Agent is AgentStatus.deploying, cannot process message."}`.
- `POST /api/agents/{id}/chat` body `{message, conversation_id?}` →
  `{"response": "<agent text>"}`. Synchronous and long-blocking (one probe
  exceeded 170s); the client needs a generous timeout (we use 600s) and the
  HTTP layer must not impose a shorter one.
- `GET /api/agents/{id}/llm-status` → `{"has_key": true, "using_maritime": true}`
  for templates without required env keys.
- Template requirements (from `/api/templates` + live probes):
  - `codex` — ready to run, no env keys.
  - `dsh` — ready to run, no env keys.
  - `claude_code` needs `ANTHROPIC_API_KEY`; `openclaw`/`hermes` need
    `OPENAI_API_KEY`; `zeroclaw` unverified. These are NOT exposed.
- Chat is conversation-threaded: passing the same `conversation_id` continues
  a thread; omitting it starts (or reuses a default) thread.
- `POST /api/agents/{id}/sleep` works; agent objects expose
  `totalComputeSeconds` (VM compute usage).

## 3. Architecture

### 3.1 MaritimeAgentClient (`app/sandbox/maritime_agent.py`, new)

Wraps the Maritime REST API, sharing the existing `httpx.AsyncClient`
(base_url + maritime auth). Methods:

- `create(name, template_id) -> str` (agent id); validated `template_id`.
- `wait_active(agent_id, timeout_s=180, poll=5)` — polls `GET /api/agents/{id}`
  until `status == "active"`; raises `AgentTimeout` on expiry.
- `chat(agent_id, message, conversation_id) -> str` — POSTs and returns
  `response`. Uses `with_retry` for transient failures; timeout 600s.
- `llm_status(agent_id) -> dict`.
- `sleep(agent_id)`.

The existing `MaritimeSandbox` stays as-is for repo provisioning and diff
harvesting — but it now provisions into the SAME agent that hosts the
harness. The harness's VM *is* the repo workspace.

### 3.2 One agent per task

At task start, `RunManager`/graph startup:

1. Create one Maritime agent with the task's `template_id`
   (name `aw-task-{task_id}`).
2. `wait_active`.
3. Provision the repo clone + venv inside it via the existing
   `MaritimeSandbox._provision` script (the sandbox binds to this agent id
   instead of creating its own).
4. All stage nodes chat with this agent, each stage using its own
   `conversation_id` (`aw-{task_id}-{stage}`) so stage threads stay separate.
5. On run end (`done`, `failed`, `needs_human`), sleep the agent.

Retry loops (tester → coding re-entry) reuse the same agent and same stage
conversation ids, so the harness keeps context across rounds. A stage that
needs a clean slate (e.g. after `restart`) starts a new `conversation_id`
suffixed with the retry count.

### 3.3 Stage nodes (rewrite)

All nodes move from `services.llm.chat(...)` to
`services.agent.chat(agent_id, prompt, conversation_id)`:

- **Planner** — one chat turn; response text is the plan.
- **Researcher** — unchanged browser flow via `MaritimeComputers`; its two
  LLM turns go through chat (same research conversation).
- **Coding** — prompt describes the plan, research notes, and any
  test-failure/review context, and instructs the harness to edit the repo in
  the workspace (`/data/workspace`) using its own tools and to finish by
  leaving uncommitted changes. After the chat reply, the orchestrator
  harvests `git diff --cached` itself (unchanged code path). Empty diff keeps
  today's `needs_human` behavior. Multi-round op-parsing JSON protocol is
  REMOVED — the harness works autonomously in one chat turn (long timeout).
- **Tester** — prompt runs tests via instruction; orchestrator still executes
  tests itself via `sb.run_long` and passes failing output to the harness for
  triage only if needed. Verdict JSON (passed/failing output/tests) is parsed
  from orchestrator-run tests, NOT the chat, keeping routing deterministic.
  Chat is used to summarize failures for the coding stage's context.
- **Reviewer** — one chat turn over the diff; response parsed into comments
  (existing parse/repair flow).

### 3.4 Cost tracking

- OpenRouter `PriceTable` and token-cost math are removed from the graph.
- LLM cost becomes Maritime compute: at run end (and on `cost_update` emits at
  stage boundaries) read the agent's `totalComputeSeconds`, multiply by the
  configured VM cost rate, and report as `vm_cost`. `llm_cost` is reported as
  0 (Maritime bills via wallet, not exposed per-call — verified no usage field
  in chat response).
- `CostTracker` keeps Redis accumulation; `cost_so_far` = VM cost only.

### 3.5 API changes

- New `GET /templates` → `{templates: [{id, name, description, tags}]}` from a
  static allowlist `["codex", "dsh"]` (fetched description metadata may be
  cached from Maritime `/api/templates` at startup; fall back to static text).
- `POST /tasks` body: `template_id` REQUIRED, validated against the allowlist
  (422 otherwise). `model_overrides` field REMOVED (unknown fields ignored by
  Pydantic config anyway).
- TaskState: `model_overrides` field removed from new states; old persisted
  states with the field still render (field ignored on read).

### 3.6 Frontend

- Create page: repo dropdown unchanged; per-stage model fieldset REMOVED;
  new single **Agent template** SearchableSelect fed by `GET /templates`
  (label = name, hint = description, default `codex`).
- Task detail: no changes (stage timeline, SSE events, artifacts all keep
  working; `model_overrides` no longer displayed).

### 3.7 Config

- Removed: `model_planner`, `model_researcher`, `model_coding_agent`,
  `model_reviewer`, OpenRouter client usage in `Services`.
- `maritime_template_id` remains as the fallback default (`codex`).
- `openrouter_*` settings remain (harmless; price table no longer fetched).

## 4. Error handling

- Agent create/wait failure → task `failed` with error_log entry (same
  pattern as sandbox provisioning failure today).
- Chat transient errors → `with_retry` (existing backoff).
- Chat malformed/no response for tester verdict → orchestrator-side test
  execution makes verdicts deterministic; harness reply is advisory only.
- Empty coding diff after harness claims done → `needs_human` (unchanged).
- Template not in allowlist → 422 on task creation.

## 5. Testing

- Unit: `MaritimeAgentClient` against a stubbed httpx transport (create,
  wait_active success/timeout, chat response extraction, retry).
- Node tests: existing StubLLM replaced by StubAgent (scripted responses);
  planner/researcher/coding/tester/reviewer tests updated to assert chat calls
  and conversation ids.
- Route tests: `POST /tasks` template validation; `GET /templates`.
- End-to-end: rebuild frontend, restart server, run one real task on
  `demo/sample-repo` with `codex`, verify plan → diff → PR flow.

## 6. Out of scope

- Per-stage templates or per-stage models (explicitly rejected).
- Templates requiring user-supplied API keys (`claude_code`, `openclaw`,
  `hermes`).
- OpenRouter billing/price table (removed).
