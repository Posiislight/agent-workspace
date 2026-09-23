import json

from app.events.publisher import Event


async def sse_events(redis, task_id: str, prefix: str = "aw"):
    """Yield SSE strings for all events on the task's stream, then keep following."""
    key = f"{prefix}:{task_id}:events"
    last_id = "0"
    while True:
        resp = await redis.xread({key: last_id}, block=15000, count=100)
        if not resp:
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                last_id = entry_id
                event = Event(task_id=fields["task_id"], node=fields["node"],
                              type=fields["type"], data=json.loads(fields["data"]),
                              ts=fields["ts"])
                yield event.to_sse()
