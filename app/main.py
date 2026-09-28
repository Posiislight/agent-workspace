import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import routes_webhooks
from app.api.routes_tasks import router
from app.config import get_settings
from app.db import ensure_schema, make_checkpointer
from app.events.publisher import Event, EventPublisher
from app.github.client import GitHubClient
from app.graph.build import build_graph
from app.graph.nodes.helpers import Services
from app.llm.openrouter import OpenRouterClient
from app.llm.pricing import PriceTable
from app.sandbox.computers import MaritimeComputers
from app.sandbox.workspace import WorkspaceRegistry
from app.services.leases import LeasePool
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
        publisher = EventPublisher(redis)
        pool = LeasePool(settings.max_awake_vms, settings.max_running_computers)

        async def _ws_emit(task_id, type, data):
            await publisher.publish(Event(task_id=task_id, node="workspace", type=type,
                                          data=data))

        # One persistent, sleeping VM per (repo, template, slot) — phase-4 §6.
        workspaces = WorkspaceRegistry(settings, http_maritime, pool, emit=_ws_emit)

        svc = Services(
            settings=settings,
            llm=OpenRouterClient(settings.openrouter_api_key, settings.openrouter_base_url, http_open),
            prices=prices,
            publisher=publisher,
            sandbox_factory=workspaces,
            computers=MaritimeComputers(settings, http_maritime),
            redis=redis,
            pg_dsn=settings.database_url,
            github=GitHubClient(settings.github_pat),
            pool=pool,
            workspaces=workspaces,
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
        await svc.github.aclose()
        await cm.__aexit__(None, None, None)

    app = FastAPI(title="agent-workspace", lifespan=lifespan)
    if services is not None:
        app.state.services = services
        app.state.run_manager = RunManager(services, graph)
    app.include_router(router)
    app.include_router(routes_webhooks.router)

    @app.get("/workspaces")
    async def workspaces_view():
        """Lease pool + persistent repo agents (awake/sleeping, owner)."""
        svc = app.state.services
        pool = getattr(svc, "pool", None)
        reg = getattr(svc, "workspaces", None)
        return {
            "max_awake_vms": getattr(pool, "max_vms", None),
            "max_running_computers": getattr(pool, "max_computers", None),
            "awake": pool.awake if pool else {},
            "computers": sorted(pool.computer_owners) if pool else [],
            "agents": reg.snapshot() if reg and hasattr(reg, "snapshot") else [],
        }

    # Serve the production frontend build (app/frontend/dist) at "/".
    # Registered after the API router so /tasks* keeps priority.
    dist = Path(__file__).resolve().parent / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    return app


app = create_app()