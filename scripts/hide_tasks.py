"""Hide tasks from the default task list (they stay in the DB and at /tasks/{id}).

    python scripts/hide_tasks.py --repo org/repo            # integration-test runs
    python scripts/hide_tasks.py --id 91ce4302... --id ...  # specific tasks
    python scripts/hide_tasks.py --repo org/repo --unhide   # undo
"""
import argparse
import asyncio

from app.config import Settings
from app.db import ensure_schema, set_hidden


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", action="append", default=[], help="task id (repeatable)")
    ap.add_argument("--repo", action="append", default=[], help="org/name (repeatable)")
    ap.add_argument("--status", action="append", help="only rows with this status")
    ap.add_argument("--unhide", action="store_true")
    args = ap.parse_args()
    if not args.id and not args.repo:
        ap.error("pass at least one --id or --repo")
    dsn = Settings().database_url
    await ensure_schema(dsn)
    n = await set_hidden(dsn, hidden=not args.unhide, task_ids=args.id, repos=args.repo,
                         statuses=args.status)
    print(f"{'unhid' if args.unhide else 'hid'} {n} task(s)")


if __name__ == "__main__":
    asyncio.run(main())
