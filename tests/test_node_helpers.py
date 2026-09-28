from tests.fakes import FakeRedis, make_services


async def test_make_services_autowires_tracker_from_redis():
    s = make_services(redis=FakeRedis())
    from app.services.cost_tracker import CostTracker
    assert isinstance(s.cost, CostTracker)


async def test_conversation_id_plain_and_retry():
    from app.graph.nodes.helpers import conversation_id
    assert conversation_id("t1", "planner", 0) == "aw-t1-planner"
    assert conversation_id("t1", "coding", 2) == "aw-t1-coding-r2"
