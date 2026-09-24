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


class RunManager:
    def __init__(self, services, graph):
        self.services = services
        self.graph = graph

    def _config(self, task_id):
        return {"configurable": {"thread_id": task_id}}

    async def prepare(self, task_id, values):
        await self._mirror(task_id, values)

    async def start(self, task_id, initial_state_values):
        return asyncio.create_task(self.drive(task_id, initial_state_values))

    async def drive(self, task_id, graph_input):
        lock_key = f"aw:{task_id}:driving"
        if self.services.redis is not None:
            got = await self.services.redis.set(lock_key, "1", nx=True, ex=3600)
            if not got:
                raise AlreadyRunning(task_id)
        try:
            config = {"configurable": {"thread_id": task_id}}
            async for chunk in self.graph.astream(graph_input, config, stream_mode="updates"):
                snap = await self.graph.aget_state(config)
                values = dict(snap.values) if snap and snap.values else {}
                if "__interrupt__" in chunk:
                    # No aupdate_state here: any as_node-less update on an interrupted
                    # thread replaces the task's pending interrupt write, which would
                    # make Command(resume=...) a no-op. paused_at/status live in the
                    # Redis/PG mirror and are stamped into graph state at resume time.
                    values["paused_at"] = now_iso()
                    values["status"] = "awaiting_approval"
                    await self._mirror(task_id, values)
                    await self._emit(task_id, "human_approval", "node_started",
                                     {"paused_at": values["paused_at"], "paused": True})
                    try:
                        await self.services.sandbox_factory(values).sleep()
                    except Exception:  # noqa: BLE001, S110 - sleeping is best-effort
                        pass
                else:
                    await self._mirror(task_id, values)
        finally:
            if self.services.redis is not None:
                await self.services.redis.delete(lock_key)

    async def resume(self, task_id, decision, feedback=None):
        state = await self.get_state(task_id)
        if not state:
            raise TaskNotFound(task_id)
        if state.get("status") != "awaiting_approval":
            raise NotAwaitingApproval(task_id)
        resumed = now_iso()
        paused = state.get("paused_at")
        latency = None
        if paused:
            latency = round((datetime.fromisoformat(resumed)
                             - datetime.fromisoformat(paused)).total_seconds(), 3)
        await self._emit(task_id, "human_approval", "sleep_wake",
                         {"paused_at": paused, "resumed_at": resumed,
                          "resume_latency_seconds": latency, "decision": decision})
        payload = {"decision": decision, "feedback": feedback or ""}
        return asyncio.create_task(
            self.drive(task_id, Command(resume=payload,
                                        update={"paused_at": paused,
                                                "resumed_at": resumed})))

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
                              values.get("status", ""), paused_at=values.get("paused_at"),
                              resumed_at=values.get("resumed_at"),
                              cost_so_far=values.get("cost_so_far"),
                              retry_counts=values.get("retry_counts"),
                              pr_url=values.get("pr_url"))

    async def _emit(self, task_id, node, type, data):
        await self.services.publisher.publish(
            Event(task_id=task_id, node=node, type=type, data=data))