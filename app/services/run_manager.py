import asyncio
import json
from datetime import datetime

from langgraph.types import Command

from app.db import upsert_task
from app.events.publisher import Event
from app.graph.nodes.helpers import now_iso


class AlreadyRunning(RuntimeError):
    pass


class NotAwaitingApproval(RuntimeError):
    pass


class TaskNotFound(RuntimeError):
    pass


class FollowupNotAllowed(RuntimeError):
    pass


FOLLOWUP_STATUSES = ("awaiting_approval", "done", "needs_human")


class RunManager:
    def __init__(self, services, graph):
        self.services = services
        self.graph = graph
        self._drives: dict[str, asyncio.Task] = {}
        self._resume_spawns: set[str] = set()

    def _config(self, task_id):
        return {"configurable": {"thread_id": task_id}}

    async def prepare(self, task_id, values):
        await self._mirror(task_id, values)

    def _spawn(self, task_id, coro):
        task = asyncio.create_task(coro)
        self._drives[task_id] = task
        task.add_done_callback(lambda t: self._on_drive_done(task_id, t))
        return task

    def _on_drive_done(self, task_id, task):
        if self._drives.get(task_id) is task:
            self._drives.pop(task_id, None)
        self._resume_spawns.discard(task_id)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        asyncio.create_task(
            self._emit(task_id, "run_manager", "node_completed",
                       {"error": repr(exc)}))
        asyncio.create_task(self._mirror_crash(task_id, exc))

    async def _mirror_crash(self, task_id, exc):
        try:
            state = await self.get_state(task_id) or {}
            await self._mirror(task_id, {
                **state,
                "status": "failed",
                "error_log": list(state.get("error_log") or [])
                + [f"drive crashed: {exc!r}"],
            })
        except Exception:  # noqa: BLE001 - best-effort mirror; callback must not raise
            pass

    async def start(self, task_id, initial_state_values):
        return self._spawn(task_id, self.drive(task_id, initial_state_values))

    async def drive(self, task_id, graph_input):
        lock_key = f"aw:{task_id}:driving"
        if self.services.redis is not None:
            got = await self.services.redis.set(lock_key, "1", nx=True, ex=3600)
            if not got:
                raise AlreadyRunning(task_id)
        try:
            config = {"configurable": {"thread_id": task_id}}
            paused = False
            async for chunk in self.graph.astream(graph_input, config, stream_mode="updates"):
                snap = await self.graph.aget_state(config)
                values = dict(snap.values) if snap and snap.values else {}
                if "__interrupt__" in chunk:
                    # The PR is opened inside the human_approval node before the
                    # interrupt fires, but its fields only land in graph state at
                    # resume time — so pull them from the interrupt payload here,
                    # otherwise the mirror has no pr_number and the webhook
                    # route cannot resolve PR -> task.
                    kind = "approval"
                    for intr in chunk["__interrupt__"]:
                        value = getattr(intr, "value", None) or {}
                        kind = value.get("kind", kind)
                        pr = value.get("pr")
                        if pr:
                            values["pr_url"] = pr["html_url"]
                            values["pr_number"] = pr["number"]
                    # No aupdate_state here: any as_node-less update on an interrupted
                    # thread replaces the task's pending interrupt write, which would
                    # make Command(resume=...) a no-op. paused_at/status live in the
                    # Redis/PG mirror and are stamped into graph state at resume time.
                    values["paused_at"] = now_iso()
                    values["status"] = ("awaiting_budget" if kind == "budget"
                                        else "awaiting_approval")
                    # A paused drive can never take another step, so vacate the
                    # tracking slot AND release the driving lock BEFORE the
                    # mirror becomes observable: any resume that sees
                    # awaiting_approval then acquires the lock and resumes
                    # correctly, instead of hitting AlreadyRunning ->
                    # _mirror_crash -> status "failed" (which would brick the
                    # task: every later resume raises NotAwaitingApproval).
                    self._drives.pop(task_id, None)
                    if self.services.redis is not None:
                        try:
                            await self.services.redis.delete(lock_key)
                        except Exception:  # noqa: BLE001, S110 - best-effort
                            pass
                    await self._mirror(task_id, values)
                    await self._emit(task_id, "human_approval", "node_started",
                                     {"paused_at": values["paused_at"], "paused": True,
                                      "kind": kind})
                    await self._sleep_workspace(values)
                    paused = True
                else:
                    await self._mirror(task_id, values)
        finally:
            if not paused:
                # Done / needs_human / crashed: the VM goes back to sleep and its
                # lease returns to the pool (phase-4 §4).
                try:
                    snap = await self.graph.aget_state({"configurable": {"thread_id": task_id}})
                    await self._sleep_workspace(dict(snap.values) if snap and snap.values
                                                else {})
                except Exception:  # noqa: BLE001, S110 - best-effort
                    pass
            if self.services.redis is not None and not paused:
                await self.services.redis.delete(lock_key)

    async def _sleep_workspace(self, values):
        if not values.get("repo"):
            return
        try:
            await self.services.sandbox_factory(values).sleep()
        except Exception:  # noqa: BLE001, S110 - sleeping is best-effort
            pass

    async def resume(self, task_id, decision, feedback=None):
        live = self._drives.get(task_id)
        if live is not None and not live.done() and task_id in self._resume_spawns:
            raise AlreadyRunning(task_id)
        # Claim the resume marker BEFORE any await: a second resume arriving
        # during the get_state/emit window must also hit the guard above,
        # instead of sneaking through while the first resume is still awaiting.
        self._resume_spawns.add(task_id)
        state = await self.get_state(task_id)
        if not state:
            self._resume_spawns.discard(task_id)
            raise TaskNotFound(task_id)
        allowed = (("awaiting_approval", "awaiting_budget") if decision == "rejected"
                   else ("awaiting_approval",))
        if state.get("status") not in allowed:
            self._resume_spawns.discard(task_id)
            raise NotAwaitingApproval(task_id)
        return await self._resume_with(task_id, state, {"decision": decision,
                                                        "feedback": feedback or ""})

    async def raise_budget(self, task_id, budget_usd: float):
        live = self._drives.get(task_id)
        if live is not None and not live.done() and task_id in self._resume_spawns:
            raise AlreadyRunning(task_id)
        self._resume_spawns.add(task_id)
        state = await self.get_state(task_id)
        if not state:
            self._resume_spawns.discard(task_id)
            raise TaskNotFound(task_id)
        if state.get("status") != "awaiting_budget":
            self._resume_spawns.discard(task_id)
            raise NotAwaitingApproval(task_id)
        return await self._resume_with(task_id, state, {"decision": "raise_budget",
                                                        "budget_usd": float(budget_usd)})

    async def followup(self, task_id, instruction: str, source: str = "chat",
                       extra: dict | None = None):
        """Wake the task's agent with a new instruction on the same branch (phase-4 §7)."""
        live = self._drives.get(task_id)
        if live is not None and not live.done():
            raise AlreadyRunning(task_id)
        if task_id in self._resume_spawns:
            raise AlreadyRunning(task_id)
        self._resume_spawns.add(task_id)
        state = await self.get_state(task_id)
        if not state:
            self._resume_spawns.discard(task_id)
            raise TaskNotFound(task_id)
        status = state.get("status")
        if status not in FOLLOWUP_STATUSES:
            self._resume_spawns.discard(task_id)
            raise FollowupNotAllowed(f"task is {status}")
        if status == "done" and self.services.settings.merge_pr_when_ready:
            self._resume_spawns.discard(task_id)
            raise FollowupNotAllowed("PR was merged (MERGE_PR_WHEN_READY=true)")
        request = {"source": source, "instruction": instruction, "at": now_iso()}
        history = list(state.get("followups") or []) + [request]
        update = {"followups": history, **(extra or {})}
        await self._emit(task_id, "followup", "followup",
                         {"source": source, "instruction": instruction[:500], "from": status})
        if status == "awaiting_approval":
            return await self._resume_with(
                task_id, state, {"decision": "followup", "feedback": instruction,
                                 "request": request}, extra_update=update)
        # Finished thread: new input starts at START, which routes to coding_agent.
        graph_input = {**update, "followup_request": request, "status": "coding",
                       "approval_status": "pending",
                       "retry_counts": {"testing": 0, "coding": 0}}
        await self._mirror(task_id, {**state, **graph_input})
        return self._spawn(task_id, self.drive(task_id, graph_input))

    async def _resume_with(self, task_id, state, payload, extra_update=None):
        resumed = now_iso()
        paused = state.get("paused_at")
        latency = None
        if paused:
            latency = round((datetime.fromisoformat(resumed)
                             - datetime.fromisoformat(paused)).total_seconds(), 3)
        await self._emit(task_id, "human_approval", "sleep_wake",
                         {"paused_at": paused, "resumed_at": resumed,
                          "resume_latency_seconds": latency,
                          "decision": payload.get("decision")})
        return self._spawn(
            task_id, self.drive(task_id, Command(resume=payload,
                                                 update={"paused_at": paused,
                                                         "resumed_at": resumed,
                                                         **(extra_update or {})})))

    async def get_state(self, task_id):
        if self.services.redis is not None:
            raw = await self.services.redis.get(f"aw:{task_id}:state")
            if raw:
                return json.loads(raw)
        snap = await self.graph.aget_state(self._config(task_id))
        return dict(snap.values) if snap and snap.values else None

    async def artifacts(self, task_id):
        st = await self.get_state(task_id) or {}
        return {k: st.get(k) for k in ("plan", "research_notes", "code_diff",
                                       "test_results", "review_comments")}

    async def _mirror(self, task_id, values):
        if not values:
            return
        if self.services.redis is not None:
            await self.services.redis.set(f"aw:{task_id}:state", json.dumps(values))
        if self.services.pg_dsn:
            await upsert_task(self.services.pg_dsn, task_id, values.get("repo", ""),
                              values.get("status", ""),
                              description=values.get("task_description"),
                              paused_at=values.get("paused_at"),
                              resumed_at=values.get("resumed_at"),
                              cost_so_far=values.get("cost_so_far"),
                              retry_counts=values.get("retry_counts"),
                              pr_url=values.get("pr_url"),
                              pr_number=values.get("pr_number"))

    async def _emit(self, task_id, node, type, data):
        await self.services.publisher.publish(
            Event(task_id=task_id, node=node, type=type, data=data))