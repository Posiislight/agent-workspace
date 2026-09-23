import json
import uuid

import pytest

from app.events.publisher import Event, EventPublisher
from app.events.sse import sse_events

pytestmark = pytest.mark.integration


async def test_publish_then_stream_replays(redis_client):
    task_id = f"tX-{uuid.uuid4().hex[:8]}"
    pub = EventPublisher(redis_client)
    for i in range(3):
        await pub.publish(Event(task_id=task_id, node="planner", type="node_completed",
                                data={"i": i}))
    collected = []
    async for sse in sse_events(redis_client, task_id):
        collected.append(sse)
        if len(collected) == 3:
            break
    assert len(collected) == 3
    payload = json.loads(collected[0].split("data: ", 1)[1].strip().rsplit("\n", 1)[0])
    assert payload["type"] == "node_completed" and payload["task_id"] == task_id
    await redis_client.delete(f"aw:{task_id}:events")
