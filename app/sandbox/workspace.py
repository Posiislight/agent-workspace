"""Persistent repo agents (spec phase-4 §6).

One Maritime VM per (repo, template, slot) that sleeps between tasks, and a
per-task `TaskWorkspace` view that leases it, switches it onto the task's
branch, and keeps deps warm. `TaskWorkspace` exposes the same interface as
`MaritimeSandbox`, so graph nodes are unchanged.
"""
import shlex
import time
from collections.abc import Awaitable, Callable

import httpx

from app.github.push import branch_for
from app.sandbox.base import ExecResult
from app.sandbox.maritime import VENV, WORKSPACE, MaritimeSandbox

AW_DIR = "/data/.aw"
MEMORY_FILE = f"{AW_DIR}/memory.md"
DEPS_SHA = f"{AW_DIR}/deps.sha"

EmitFn = Callable[[str, str, dict], Awaitable[None]]  # (task_id, type, data)


def template_for(settings, harness: str | None) -> str:
    h = harness or settings.default_harness
    if h == "dsh":
        return settings.maritime_template_dsh
    if h == "codex":
        return settings.maritime_template_codex
    return settings.maritime_template_id


def agent_key(repo: str, template: str, slot: int) -> str:
    return f"{repo}:{template}:{slot}"


def _slug(repo: str) -> str:
    return repo.replace("/", "-").replace(".", "-").lower()


def activation_script(repo: str, task_id: str, base: str, pat: str) -> str:
    """Put the shared workspace on `aw/{task_id}`, parking the previous task's WIP
    on its own branch. Idempotent; safe after a VM recreate (restores the branch
    from the remote when it was already pushed)."""
    br = shlex.quote(branch_for(task_id))
    b = shlex.quote(base)
    url = shlex.quote(f"https://x-access-token:{pat}@github.com/{repo}.git")
    return (
        "set -e\n"
        f"cd {WORKSPACE}\n"
        f"git fetch -q {url} '+refs/heads/{base}:refs/remotes/origin/{base}'\n"
        f"git fetch -q {url} '+refs/heads/aw/{task_id}:refs/remotes/origin/aw/{task_id}' "
        "2>/dev/null || true\n"
        "cur=$(git rev-parse --abbrev-ref HEAD)\n"
        f"if [ \"$cur\" != {br} ]; then\n"
        "  git add -A\n"
        "  if ! git diff --cached --quiet; then\n"
        "    case \"$cur\" in aw/*) git commit -qm 'aw: wip (parked)' ;; "
        "*) git reset -q --hard ;; esac\n"
        "  fi\n"
        f"  if git show-ref --verify --quiet refs/heads/{br}; then git checkout -q {br}\n"
        f"  elif git show-ref --verify --quiet refs/remotes/origin/{br}; then "
        f"git checkout -q -B {br} origin/{br}\n"
        f"  else git checkout -q -B {br} origin/{b}; fi\n"
        "  echo SWITCHED\n"
        "fi\n"
        # Local base ref follows the remote so new branches start fresh; diffs use
        # the merge-base, so moving it never pollutes an older task's diff.
        f"git branch -f {b} origin/{b} 2>/dev/null || true\n"
    )


def deps_script() -> str:
    req = f"{WORKSPACE}/requirements.txt"
    return (
        f"if [ -f {req} ] && [ -x {VENV}/bin/pip ]; then "
        f"h=$(sha256sum {req} | cut -d' ' -f1); "
        f"if [ \"$h\" != \"$(cat {DEPS_SHA} 2>/dev/null)\" ]; then "
        f"{VENV}/bin/pip install -q -r {req} && mkdir -p {AW_DIR} && echo $h > {DEPS_SHA} "
        f"&& echo DEPS=installed; else echo DEPS=cached; fi; "
        f"else echo DEPS=none; fi"
    )


class TaskWorkspace:
    """A task's leased view onto a persistent repo agent."""

    def __init__(self, agent: MaritimeSandbox, pool, *, task_id: str, base_branch: str,
                 key: str, pat: str, emit: EmitFn | None = None, slot: int = 0,
                 harness: str = "openrouter"):
        self.agent = agent
        self.pool = pool
        self.task_id = task_id
        self.base_branch = base_branch
        self.repo = agent.repo
        self.key = key
        self.slot = slot
        self.harness = harness
        self._pat = pat
        self._emit = emit
        self._ready = False
        self._awake_since: float | None = None
        self._vm_seconds = 0.0
        self.last_metrics: dict | None = None
        self._pending_metrics: list[dict] = []

    @property
    def agent_id(self):
        return self.agent.agent_id

    async def _event(self, type: str, data: dict):
        if self._emit is not None:
            await self._emit(self.task_id, type, data)

    async def ensure(self) -> str:
        if self._ready and self.pool.holds_vm(self.key, self.task_id):
            return self.agent.agent_id

        async def _queued():
            await self._event("tool_call", {"action": "queued_for_vm", "agent": self.key,
                                            "awake": list(self.pool.awake)})
        t0 = time.monotonic()
        await self.pool.acquire_vm(self.key, self.task_id, on_wait=_queued)
        t_lease = time.monotonic()
        if self._awake_since is None:
            self._awake_since = t_lease
        try:
            await self.agent.ensure()
            cold = self.agent.last_created
            switched = False
            deps = "skipped"
            if self.agent.active_task != self.task_id or cold:
                res = await self.agent.exec(
                    activation_script(self.repo, self.task_id, self.base_branch, self._pat))
                if res.exit_code != 0:
                    raise RuntimeError("workspace activation failed: "
                                       + res.combined.replace(self._pat, "***")[:1500])
                switched = "SWITCHED" in res.stdout
                dres = await self.agent.run_long(deps_script())
                deps = next((ln.split("=", 1)[1] for ln in dres.stdout.splitlines()
                             if ln.startswith("DEPS=")), "unknown")
                self.agent.active_task = self.task_id
                self.last_metrics = {
                    "agent": self.key, "cold": cold, "switched_branch": switched,
                    "deps": deps, "queued_seconds": round(t_lease - t0, 3),
                    "ready_seconds": round(time.monotonic() - t_lease, 3),
                }
                self._pending_metrics.append(self.last_metrics)
                await self._event("workspace_ready", self.last_metrics)
        except Exception:
            await self.sleep()
            raise
        self._ready = True
        return self.agent.agent_id

    async def _ensure_ready(self):
        if not (self._ready and self.pool.holds_vm(self.key, self.task_id)):
            await self.ensure()

    async def exec(self, command, timeout=None) -> ExecResult:
        await self._ensure_ready()
        return await self.agent.exec(command, timeout=timeout)

    async def run_long(self, command: str, timeout=None) -> ExecResult:
        await self._ensure_ready()
        return await self.agent.run_long(command, timeout=timeout)

    async def read_file(self, path: str) -> str:
        await self._ensure_ready()
        return await self.agent.read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        await self._ensure_ready()
        await self.agent.write_file(path, content)

    async def list_files(self, path: str) -> list[dict]:
        await self._ensure_ready()
        return await self.agent.list_files(path)

    async def diff(self) -> str:
        """Cumulative diff (commits + worktree) against the fork point from base."""
        res = await self.exec(
            f"cd {WORKSPACE} && git add -A && "
            f"git diff --cached $(git merge-base HEAD {shlex.quote(self.base_branch)})")
        return res.stdout

    async def shortstat(self) -> dict:
        res = await self.exec(
            f"cd {WORKSPACE} && git add -A && git diff --cached --shortstat "
            f"$(git merge-base HEAD {shlex.quote(self.base_branch)})")
        return parse_shortstat(res.stdout)

    async def read_memory(self) -> str:
        res = await self.exec(f"tail -c 4000 {MEMORY_FILE} 2>/dev/null || true")
        return res.stdout.strip()

    async def append_memory(self, line: str) -> None:
        one = " ".join(line.split())[:400]
        await self.exec(f"mkdir -p {AW_DIR} && printf '%s\\n' {shlex.quote(one)} >> {MEMORY_FILE}")

    def pop_metrics(self) -> list[dict]:
        out, self._pending_metrics = self._pending_metrics, []
        return out

    def take_vm_seconds(self) -> float:
        """Awake seconds accrued since the last call (billing attribution)."""
        total = self._vm_seconds
        if self._awake_since is not None:
            now = time.monotonic()
            total += now - self._awake_since
            self._awake_since = now
        self._vm_seconds = 0.0
        return total

    async def sleep(self) -> None:
        if self._awake_since is not None:
            self._vm_seconds += time.monotonic() - self._awake_since
            self._awake_since = None
        self._ready = False
        try:
            if self.pool.holds_vm(self.key, self.task_id):
                await self.agent.sleep()
        finally:
            await self.pool.release_vm(self.key, self.task_id)


def parse_shortstat(text: str) -> dict:
    import re
    out = {"files": 0, "insertions": 0, "deletions": 0}
    for n, word in re.findall(r"(\d+) (file|insertion|deletion)", text or ""):
        out[{"file": "files", "insertion": "insertions", "deletion": "deletions"}[word]] = int(n)
    return out


class WorkspaceRegistry:
    """sandbox_factory for the app: caches repo agents and per-task workspaces."""

    def __init__(self, settings, client: httpx.AsyncClient, pool, emit: EmitFn | None = None):
        self._s = settings
        self._client = client
        self.pool = pool
        self._emit = emit
        self._agents: dict[str, MaritimeSandbox] = {}
        self._workspaces: dict[tuple[str, str], TaskWorkspace] = {}

    def agent(self, repo: str, base_branch: str, template: str, slot: int) -> MaritimeSandbox:
        key = agent_key(repo, template, slot)
        if key not in self._agents:
            name = f"aw-repo-{_slug(repo)}-{_slug(template)}-{slot}"[:60]
            self._agents[key] = MaritimeSandbox(
                self._s, self._client, name, repo, base_branch, name=name,
                external_id=f"aw-repo:{key}", template_id=template,
                env=self._s.harness_env if template != self._s.maritime_template_id else None,
                install_deps=False)
        return self._agents[key]

    def workspace(self, state, harness: str | None = None, slot: int | None = None) -> TaskWorkspace:
        harness = harness or state.get("harness") or self._s.default_harness
        slot = state.get("workspace_slot", 0) if slot is None else slot
        template = template_for(self._s, harness)
        key = agent_key(state["repo"], template, slot or 0)
        wkey = (state["task_id"], key)
        if wkey not in self._workspaces:
            self._workspaces[wkey] = TaskWorkspace(
                self.agent(state["repo"], state["base_branch"], template, slot or 0),
                self.pool, task_id=state["task_id"], base_branch=state["base_branch"],
                key=key, pat=self._s.github_pat, emit=self._emit, slot=slot or 0,
                harness=harness)
        return self._workspaces[wkey]

    def __call__(self, state) -> TaskWorkspace:
        return self.workspace(state)

    def for_candidate(self, state, harness: str, slot: int) -> TaskWorkspace:
        return self.workspace(state, harness=harness, slot=slot)

    def snapshot(self) -> list[dict]:
        awake = self.pool.awake
        return [{"agent": k, "agent_id": a.agent_id, "awake": k in awake,
                 "owner": awake.get(k), "active_task": a.active_task}
                for k, a in self._agents.items()]
