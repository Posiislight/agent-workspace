import asyncio

import pytest

from app.services.run_manager import RunManager
from tests.fakes import make_services


class ExplodingGraph:
    async def astream(self, *_args, **_kw):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator


async def test_drive_failure_is_published():
    services = make_services()
    rm = RunManager(services, ExplodingGraph())
    await rm.start("t-fail", {"task_id": "t-fail"})
    with pytest.raises(RuntimeError):
        await rm._drives["t-fail"]
    await asyncio.sleep(0.01)
    errors = [e for e in services.publisher.events
              if e.node == "run_manager" and "error" in e.data]
    assert errors and "boom" in errors[-1].data["error"]
