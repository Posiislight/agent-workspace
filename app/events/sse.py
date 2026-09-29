import json

from redis.exceptions import TimeoutError as RedisTimeoutError

from app.events.publisher import Event


async def event_history(redis, task_id: str, prefix: str = "aw") -> list[dict]:
    """All events recorded on the task's stream, oldest first, as SSE payload dicts."""
    entries = await redis.xrange(f"{prefix}:{task_id}:events")
    return [{"id": entry_id, "task_id": f["task_id"], "node": f["node"],
             "type": f["type"], "data": json.loads(f["data"]), "ts": f["ts"]}
            for entry_id, f in entries]


async def sse_events(redis, task_id: str, prefix: str = "aw"):
    """Yield SSE strings for all events on the task's stream, then keep following.

    Each event carries its stream entry id as the SSE `id:` so clients can
    de-duplicate replays after a reconnect. A blocking read that trips the
    redis socket timeout yields a keepalive instead of killing the stream
    (that crash made browsers reconnect and replay every event).
    """
    key = f"{prefix}:{task_id}:events"
    last_id = "0"
    while True:
        try:
            resp = await redis.xread({key: last_id}, block=10000, count=100)
        except RedisTimeoutError:
            resp = None
        if not resp:
            yield ": keepalive\n\n"
            continue
        for _stream, entries in resp:
            for entry_id, fields in entries:
                last_id = entry_id
                event = Event(task_id=fields["task_id"], node=fields["node"],
                              type=fields["type"], data=json.loads(fields["data"]),
                              ts=fields["ts"])
                yield event.to_sse(event_id=entry_id)
