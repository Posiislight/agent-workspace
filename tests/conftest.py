import pytest

from app.config import Settings


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
async def redis_client(settings):
    import redis.asyncio as aioredis
    client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()
