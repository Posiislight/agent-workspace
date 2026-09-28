from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.events.publisher import Event


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class Services:
    settings: object
    llm: object
    prices: object
    publisher: object
    sandbox_factory: Callable
    computers: object
    redis: object
    pg_dsn: str | None = None
    github: object = None
    pool: object = None                 # LeasePool (phase 4)
    workspaces: object = None           # WorkspaceRegistry (candidates, snapshot)
    clock: Callable[[], str] = field(default_factory=lambda: now_iso)


async def emit(services, state, node: str, type: str, data: dict):
    await services.publisher.publish(
        Event(task_id=state["task_id"], node=node, type=type, data=data))


def model_for(services, state, role: str) -> str:
    override = (state.get("model_overrides") or {}).get(role)
    if override:
        return override
    return getattr(services.settings, f"model_{role}")


async def chat_with_retry(services, state, role: str, messages: list[dict]):
    from app.llm.backoff import with_retry
    return await with_retry(
        lambda: services.llm.chat(model_for(services, state, role), messages),
        attempts=services.settings.backoff_attempts,
        base_delay=services.settings.backoff_base_delay)


async def apply_llm_cost(updates: dict, state, services, result, node: str) -> dict:
    delta = services.prices.cost(result.model, result.prompt_tokens, result.completion_tokens)
    merged = {**state, **updates}
    updates["llm_cost"] = round((merged.get("llm_cost") or 0.0) + delta, 6)
    updates["cost_so_far"] = round((merged.get("cost_so_far") or 0.0) + delta, 6)
    return await apply_vm_cost(updates, state, services, node)


async def apply_vm_cost(updates: dict, state, services, node: str, sandbox=None) -> dict:
    """Fold the workspace's awake seconds into the task's cost (phase-4 §6)."""
    merged = {**state, **updates}
    secs = 0.0
    sb = sandbox
    if sb is None and merged.get("repo"):
        try:
            sb = services.sandbox_factory(merged)
        except Exception:  # noqa: BLE001 - cost accounting must never break a node
            sb = None
    take = getattr(sb, "take_vm_seconds", None)
    if take is not None:
        secs = take()
    pop = getattr(sb, "pop_metrics", None)
    new_metrics = pop() if pop is not None else []
    if new_metrics:
        updates["workspace_metrics"] = list(merged.get("workspace_metrics") or []) + new_metrics
    rate = getattr(services.settings, "vm_cost_per_minute", 0.0) or 0.0
    delta = secs / 60.0 * rate
    updates["vm_seconds"] = round((merged.get("vm_seconds") or 0.0) + secs, 3)
    updates["vm_cost"] = round((merged.get("vm_cost") or 0.0) + delta, 6)
    total = round((merged.get("cost_so_far") or 0.0) + delta, 6)
    updates["cost_so_far"] = total
    await emit(services, {**merged, "cost_so_far": total}, node, "cost_update",
               {"cost_so_far": total, "llm_cost": merged.get("llm_cost") or 0.0,
                "vm_cost": updates["vm_cost"], "vm_seconds": updates["vm_seconds"],
                "budget_usd": merged.get("budget_usd") or 0.0})
    return updates


def over_budget(state) -> bool:
    budget = state.get("budget_usd") or 0.0
    return budget > 0 and (state.get("cost_so_far") or 0.0) >= budget


async def check_budget(state, services, node: str) -> tuple[dict, bool]:
    """Pause (interrupt) before spending more when the task is over budget.

    Returns (updates, stop): updates carry a raised budget; stop=True means the
    human chose to end the task at the cap.
    """
    from inspect import isawaitable

    from langgraph.types import interrupt

    if not over_budget(state):
        return {}, False
    await emit(services, state, node, "budget_exceeded",
               {"cost_so_far": state.get("cost_so_far"), "budget_usd": state.get("budget_usd")})
    payload = interrupt({"kind": "budget", "task_id": state["task_id"], "node": node,
                         "cost_so_far": state.get("cost_so_far"),
                         "budget_usd": state.get("budget_usd"),
                         "instructions": "Resume with {'decision': 'raise_budget', "
                                         "'budget_usd': X} or {'decision': 'rejected'}"})
    if isawaitable(payload):
        payload = await payload
    if payload.get("decision") == "raise_budget":
        return {"budget_usd": float(payload.get("budget_usd") or 0.0)}, False
    return {}, True


def budget_stop(state, node: str, updates: dict | None = None):
    from langgraph.types import Command
    err = list(state.get("error_log") or []) + [
        (f"{node}: stopped at budget cap (${state.get('cost_so_far', 0.0):.4f} of "
         f"${state.get('budget_usd', 0.0):.4f})")]
    return Command(update={**(updates or {}), "error_log": err, "status": "needs_human"},
                   goto="needs_human")
