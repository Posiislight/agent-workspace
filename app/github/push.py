class PushError(RuntimeError):
    pass


def branch_for(task_id: str) -> str:
    return f"aw/{task_id}"


async def push_branch(sandbox, repo: str, branch: str, pat: str) -> None:
    url = f"https://x-access-token:{pat}@github.com/{repo}.git"
    cmd = (
        "cd /data/workspace && git add -A && "
        "(git diff --cached --quiet || git commit -m 'aw: task changes') && "
        f"git push {url} HEAD:refs/heads/{branch} && "
        f"git remote set-url origin https://github.com/{repo}.git"
    )
    result = await sandbox.run_long(cmd)
    if result.exit_code != 0:
        raise PushError(result.combined.replace(pat, "***"))