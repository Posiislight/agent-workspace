import pytest

from app.db import ensure_schema, get_task, get_task_by_pr, upsert_task

pytestmark = pytest.mark.integration


async def test_upsert_get_roundtrip_and_idempotent_schema(settings):
    await ensure_schema(settings.database_url)
    await ensure_schema(settings.database_url)  # second run must not fail
    await upsert_task(settings.database_url, "t-db", "org/repo", "planning",
                      retry_counts={"testing": 1})
    row = await get_task(settings.database_url, "t-db")
    assert row["task_id"] == "t-db" and row["status"] == "planning"
    await upsert_task(settings.database_url, "t-db", "org/repo", "done",
                      cost_so_far=0.5, retry_counts={"testing": 0, "coding": 0},
                      paused_at="2026-09-23T01:00:00+00:00",
                      resumed_at="2026-09-23T10:00:00+00:00")
    row = await get_task(settings.database_url, "t-db")
    assert row["status"] == "done"
    assert row["cost_so_far"] == 0.5
    assert row["retry_counts"] == {"testing": 0, "coding": 0}
    assert await get_task(settings.database_url, "missing") is None


async def test_pr_number_roundtrip_and_lookup(settings):
    await ensure_schema(settings.database_url)
    await upsert_task(settings.database_url, "t-pr", "org/repo", "awaiting_approval",
                      description="d", pr_url="https://github.com/org/repo/pull/9",
                      pr_number=9)
    row = await get_task_by_pr(settings.database_url, "org/repo", 9)
    assert row is not None
    assert row["task_id"] == "t-pr"
    assert row["pr_number"] == 9
    assert await get_task_by_pr(settings.database_url, "org/repo", 1234) is None


async def test_vm_cost_columns_roundtrip(settings):
    from app.db import ensure_schema, get_task, upsert_task
    await ensure_schema(settings.database_url)
    await upsert_task(settings.database_url, "t-vm2", "org/repo", "done",
                      cost_so_far=1.25, vm_cost=1.0, vm_minutes=10.0)
    row = await get_task(settings.database_url, "t-vm2")
    assert row["vm_cost"] == 1.0
    assert row["vm_minutes"] == 10.0
