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
