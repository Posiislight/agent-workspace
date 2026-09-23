import json
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Event:
    task_id: str
    node: str
    type: str
    data: dict
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now().astimezone().isoformat()

    def to_stream_fields(self) -> dict:
        return {"task_id": self.task_id, "node": self.node, "type": self.type,
                "data": json.dumps(self.data), "ts": self.ts}

    def to_sse(self) -> str:
        payload = {"task_id": self.task_id, "node": self.node, "type": self.type,
                   "data": self.data, "ts": self.ts}
        return f"event: {self.type}\ndata: {json.dumps(payload)}\n\n"


class EventPublisher:
    def __init__(self, redis, prefix: str = "aw"):
        self._redis = redis
        self._prefix = prefix

    async def publish(self, event: Event) -> str:
        return await self._redis.xadd(f"{self._prefix}:{event.task_id}:events",
                                      event.to_stream_fields())
