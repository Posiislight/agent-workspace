import asyncio
import sys

import pytest

from app.config import Settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
async def redis_client(settings):
    import redis.asyncio as aioredis
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()
