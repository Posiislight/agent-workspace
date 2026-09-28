import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import routes_webhooks
from app.api import routes_meta
from app.api.routes_tasks import router
from app.config import get_settings
from app.db import ensure_schema, make_checkpointer
from app.events.publisher import EventPublisher
from app.graph.build import build_graph
from app.graph.nodes.helpers import Services
from app.sandbox.computers import MaritimeComputers
from app.sandbox.maritime import MaritimeSandbox, make_sandbox
from app.sandbox.maritime_agent import MaritimeAgentClient
from app.services.cost_tracker import CostTracker
from app.services.run_manager import RunManager

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
# Client-side routes that share a path with the API (/tasks/{id}) or have no
# file in dist (/new). Browser navigations to these get index.html.
_SPA_ROUTE = re.compile(r"^/(new|tasks/[^/]+)/?$")


def create_app(services=None, graph=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if services is not None:  # test wiring installed at create_app time
            yield
            return
        settings = get_settings()
        redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
        await ensure_schema(settings.database_url)
        http_maritime = httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
        cost = CostTracker(redis, settings.vm_cost_per_hour)
        sandbox_cache: dict[str, MaritimeSandbox] = {}

        def _sandbox_for(state):
            tid = state["task_id"]
            if tid not in sandbox_cache:
                sandbox_cache[tid] = make_sandbox(settings, tid, state["repo"],
                                                  state["base_branch"],
                                                  client=http_maritime,
                                                  agent_id=state.get("agent_id"))
            return sandbox_cache[tid]

        agent = MaritimeAgentClient(settings.maritime_api_key, client=http_maritime)
        svc = Services(
            settings=settings,
            publisher=EventPublisher(redis),
            sandbox_factory=_sandbox_for,
            computers=MaritimeComputers(settings, http_maritime),
            redis=redis,
            pg_dsn=settings.database_url,
            cost=cost,
            agent=agent,
        )
        cm = make_checkpointer(settings.database_url)
        checkpointer = await cm.__aenter__()
        await checkpointer.setup()
        app.state.services = svc
        app.state.run_manager = RunManager(svc, build_graph(svc, checkpointer))
        yield
        await redis.aclose()
        await http_maritime.aclose()
        await cm.__aexit__(None, None, None)

    app = FastAPI(title="agent-workspace", lifespan=lifespan)
    if services is not None:
        app.state.services = services
        app.state.run_manager = RunManager(services, graph)
    app.include_router(router)
    app.include_router(routes_meta.router)
    app.include_router(routes_webhooks.router)

    # Serve the production frontend build (app/frontend/dist) at "/".
    # Registered after the API router so /tasks* keeps priority.
    dist = FRONTEND_DIST
    if dist.is_dir():
        index = dist / "index.html"

        @app.middleware("http")
        async def spa_navigation(request: Request, call_next):
            if request.method != "GET" or not _SPA_ROUTE.match(request.url.path):
                return await call_next(request)
            # Page loads are navigations (Accept: text/html); the app's own
            # fetch() is not, so the same /tasks/{id} path still returns JSON.
            h = request.headers
            navigating = (h.get("sec-fetch-mode") == "navigate"
                          or "text/html" in h.get("accept", ""))
            if navigating and index.is_file():
                resp = FileResponse(index)
            else:
                resp = await call_next(request)
            # One URL, two representations: never let the browser answer a
            # JSON fetch from the cached HTML page (or vice versa).
            resp.headers["Vary"] = "Accept, Sec-Fetch-Mode"
            resp.headers["Cache-Control"] = "no-store"
            return resp

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    return app


app = create_app()