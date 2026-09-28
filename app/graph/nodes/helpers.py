from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.events.publisher import Event


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass
class Services:
    settings: object
    publisher: object
    sandbox_factory: Callable
    computers: object
    redis: object
    pg_dsn: str | None = None
    github: object = None
    cost: object = None
    agent: object = None
    llm: object = None
    prices: object = None
    clock: Callable[[], str] = field(default_factory=lambda: now_iso)


def conversation_id(task_id: str, stage: str, retry: int = 0) -> str:
    base = f"aw-{task_id}-{stage}"
    return base if not retry else f"{base}-r{retry}"


async def emit(services, state, node: str, type: str, data: dict):
    await services.publisher.publish(
        Event(task_id=state["task_id"], node=node, type=type, data=data))