"""Server entrypoint.

On Windows, uvicorn's asyncio loop factory is ``asyncio.EventLoop`` — which IS
the ProactorEventLoop there, constructed directly and therefore bypassing any
event-loop policy (policies only inform ``new_event_loop``). psycopg3 async
rejects the Proactor loop, so on win32 we drive ``asyncio.run`` ourselves with
``loop_factory=asyncio.SelectorEventLoop`` (psycopg's own recommendation).
"""

import asyncio
import sys

import uvicorn

if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        server.run()