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
  task_description text,
  status text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz,
  paused_at timestamptz,
  resumed_at timestamptz,
  cost_so_far float8,
  retry_counts jsonb,
  pr_url text,
  pr_number int
)"""


async def ensure_schema(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(DDL)
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_description text")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS pr_number int")
    finally:
        await conn.close()


async def upsert_task(dsn, task_id, repo, status, *, description=None, paused_at=None,
                      resumed_at=None, cost_so_far=None, retry_counts=None, pr_url=None,
                      pr_number=None):
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """INSERT INTO tasks (task_id, repo, task_description, status, updated_at,
                                   paused_at, resumed_at, cost_so_far, retry_counts, pr_url,
                                   pr_number)
               VALUES ($1, $2, $3, $4, now(), $5, $6, $7, $8::jsonb, $9, $10)
               ON CONFLICT (task_id) DO UPDATE SET
                 repo = $2, task_description = $3, status = $4, updated_at = now(),
                 paused_at = $5, resumed_at = $6, cost_so_far = $7,
                 retry_counts = $8::jsonb, pr_url = $9, pr_number = $10""",
            task_id, repo, description, status, _as_ts(paused_at), _as_ts(resumed_at),
            cost_so_far, json.dumps(retry_counts or {}), pr_url, pr_number)
    finally:
        await conn.close()


async def list_tasks(dsn, limit: int = 100):
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """SELECT task_id, repo, task_description, status, created_at, updated_at,
                      cost_so_far, pr_url
               FROM tasks ORDER BY created_at DESC LIMIT $1""", limit)
        return [dict(r) for r in rows]
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


async def get_task_by_pr(dsn, repo, pr_number):
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            """SELECT * FROM tasks
               WHERE repo = $1 AND pr_number = $2
               ORDER BY created_at DESC LIMIT 1""", repo, pr_number)
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
