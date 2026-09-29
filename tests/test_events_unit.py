import json

from app.events.publisher import Event


def test_event_stream_fields():
    e = Event(task_id="t1", node="planner", type="node_started", data={"a": 1},
              ts="2026-01-01T00:00:00Z")
    f = e.to_stream_fields()
    assert f["task_id"] == "t1"
    assert f["type"] == "node_started"
    assert json.loads(f["data"]) == {"a": 1}


def test_sse_format():
    e = Event(task_id="t1", node="planner", type="cost_update", data={"cost": 0.5}, ts="T")
    assert e.to_sse() == (
        'event: cost_update\n'
        'data: {"task_id": "t1", "node": "planner", "type": "cost_update", '
        '"data": {"cost": 0.5}, "ts": "T"}\n\n'
    )


def test_ts_autofilled():
    e = Event(task_id="t", node="n", type="tool_call", data={})
    assert e.ts  # non-empty ISO timestamp


def test_sse_includes_id_when_given():
    e = Event(task_id="t1", node="n", type="tool_call", data={}, ts="T")
    assert e.to_sse(event_id="171-0").startswith("id: 171-0\nevent: tool_call\n")


async def test_sse_stream_survives_redis_read_timeout():
    import redis.exceptions
    from app.events.sse import sse_events

    fields = {"task_id": "t1", "node": "planner", "type": "node_started",
              "data": "{}", "ts": "T"}

    class FlakyRedis:
        def __init__(self):
            self.calls = 0

        async def xread(self, streams, block=None, count=None):
            self.calls += 1
            if self.calls == 1:
                raise redis.exceptions.TimeoutError("Timeout reading from localhost:6380")
            return [("aw:t1:events", [("5-0", fields)])]

    r = FlakyRedis()
    gen = sse_events(r, "t1")
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
        if chunk.startswith("id:"):
            break
    assert chunks[0] == ": keepalive\n\n"
    assert chunks[-1].startswith("id: 5-0\n")


async def test_event_history_returns_all_entries_oldest_first():
    from app.events.sse import event_history

    class FakeRedis:
        async def xrange(self, key):
            assert key == "aw:t1:events"
            return [("1-0", {"task_id": "t1", "node": "planner", "type": "node_started",
                             "data": "{}", "ts": "T1"}),
                    ("2-0", {"task_id": "t1", "node": "tester", "type": "node_completed",
                             "data": '{"passed": true}', "ts": "T2"})]

    events = await event_history(FakeRedis(), "t1")
    assert [e["id"] for e in events] == ["1-0", "2-0"]
    assert events[1] == {"id": "2-0", "task_id": "t1", "node": "tester",
                         "type": "node_completed", "data": {"passed": True}, "ts": "T2"}
