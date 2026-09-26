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
    total = round(state.get("cost_so_far", 0.0) + delta, 6)
    updates["cost_so_far"] = total
    await emit(services, {**state, "cost_so_far": total}, node, "cost_update",
               {"cost_so_far": total})
    return updates