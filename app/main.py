from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from app.api.routes_tasks import router
from app.config import get_settings
from app.db import ensure_schema, make_checkpointer
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.nodes.helpers import Services
from app.llm.openrouter import OpenRouterClient
from app.llm.pricing import PriceTable
from app.sandbox.computers import MaritimeComputers
from app.sandbox.maritime import MaritimeSandbox, make_sandbox
from app.services.run_manager import RunManager


def create_app(services=None, graph=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:  # test wiring installed at create_app time
            yield
            return
        settings = get_settings()
        redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
        await ensure_schema(settings.database_url)
        http_open = httpx.AsyncClient(base_url=settings.openrouter_base_url, timeout=130)
        http_maritime = httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
        prices = await PriceTable.fetch(http_open)
        sandbox_cache: dict[str, MaritimeSandbox] = {}

        def _sandbox_for(state):
            tid = state["task_id"]
            if tid not in sandbox_cache:
                sandbox_cache[tid] = make_sandbox(settings, tid, state["repo"],
                                                  state["base_branch"],
                                                  client=http_maritime)
            return sandbox_cache[tid]

        svc = Services(
            settings=settings,
            llm=OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url, http_open),
            prices=prices,
            publisher=EventPublisher(redis),
            sandbox_factory=_sandbox_for,
            computers=MaritimeComputers(settings, http_maritime),
            redis=redis,
            pg_dsn=settings.database_url,
        )
        cm = make_checkpointer(settings.database_url)
        checkpointer = await cm.__aenter__()
        await checkpointer.setup()
        app.state.services = svc
        app.state.run_manager = RunManager(svc, build_graph(svc, checkpointer))
        yield
        await redis.aclose()
        await http_open.aclose()
        await http_maritime.aclose()
        await cm.__aexit__(None, None, None)

    app = FastAPI(title="agent-workspace", lifespan=lifespan)
    if services is not None:
        app.state.services = services
        app.state.run_manager = RunManager(services, graph)
    app.include_router(router)
    return app


app = create_app()