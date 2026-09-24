import json
from datetime import datetime

import asyncpg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


def _as_ts(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


DDL = """CREATE TABLE IF NOT EXISTS tasks (
  task_id text PRIMARY KEY,
  repo text,
  status text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz,
  paused_at timestamptz,
  resumed_at timestamptz,
  cost_so_far float8,
  retry_counts jsonb,
  pr_url text
)"""


async def ensure_schema(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
    finally:
        await conn.close()


async def upsert_task(dsn, task_id, repo, status, *, paused_at=None, resumed_at=None,
                      cost_so_far=None, retry_counts=None, pr_url=None):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """INSERT INTO tasks (task_id, repo, status, updated_at, paused_at, resumed_at,
                                   cost_so_far, retry_counts, pr_url)
               VALUES ($1, $2, $3, now(), $4, $5, $6, $7::jsonb, $8)
               ON CONFLICT (task_id) DO UPDATE SET
                 repo = $2, status = $3, updated_at = now(), paused_at = $4,
                 resumed_at = $5, cost_so_far = $6, retry_counts = $7::jsonb, pr_url = $8""",
            task_id, repo, status, _as_ts(paused_at), _as_ts(resumed_at), cost_so_far,
            json.dumps(retry_counts or {}), pr_url)
    finally:
        await conn.close()


async def get_task(dsn, task_id):
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        if not row:
            return None
        task = dict(row)
        if isinstance(task.get("retry_counts"), str):
            task["retry_counts"] = json.loads(task["retry_counts"])
        return task
    finally:
        await conn.close()


def make_checkpointer(dsn: str):
    """Async context manager yielding AsyncPostgresSaver; caller enters and runs setup()."""
    return AsyncPostgresSaver.from_conn_string(dsn)
