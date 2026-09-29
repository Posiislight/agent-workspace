import asyncio
import shlex
import time

import httpx

from app.llm.backoff import with_retry
from app.sandbox.base import ExecResult

RUNS_DIR = "/data/.runs"
WORKSPACE = "/data/workspace"
VENV = "/data/venv"
UV_DIR = "/data/.uv"
UV = f"{UV_DIR}/bin/uv"
# Written once the venv is known-good; venvs without it (e.g. a stdlib venv
# built on a python lacking ensurepip) are rebuilt on the next provision.
VENV_OK = f"{VENV}/.aw-ok"


class MaritimeSandbox:
    """One persistent Maritime VM per task. All repo-touching nodes share it."""

    def __init__(self, settings, client: httpx.AsyncClient, task_id: str,
                 repo: str, base_branch: str):
        self._s = settings
        self._client = client
        self.task_id = task_id
        self.repo = repo
        self.base_branch = base_branch
        self.agent_id: str | None = None
        self._provisioned = False

    def _headers(self):
        return {"Authorization": f"Bearer {self._s.maritime_api_key}"}

    async def ensure(self) -> str:
        if self.agent_id:
            r = await self._client.get(f"/api/agents/{self.agent_id}", headers=self._headers())
            if r.status_code == 200:
                if not getattr(self, "_provisioned", False):
                    await self._provision()
                    self._provisioned = True
                return self.agent_id
            self.agent_id = None  # 404: VM deleted — recreate (spec §7)
        r = await with_retry(lambda: self._client.post(
            "/api/agents", headers=self._headers(),
            json={"name": f"aw-task-{self.task_id}",
                  "templateId": self._s.maritime_template_id}))
        r.raise_for_status()
        self.agent_id = r.json()["id"]
        await self._provision()
        self._provisioned = True
        return self.agent_id

    async def _provision(self):
        repo = shlex.quote(self.repo)
        branch = shlex.quote(self.base_branch)
        clone_url = f"https://x-access-token:{self._s.github_pat}@github.com/{repo}.git"
        script = (
            f"set -e\n"
            f"if [ ! -d {WORKSPACE}/.git ]; then git clone {clone_url} {WORKSPACE} "
            f"-b {branch}; fi\n"
            # PAT scrub: the clone URL embeds the token in .git/config; reset the
            # remote to the plain HTTPS URL so it cannot be read back by the VM.
            f"git -C {WORKSPACE} remote set-url origin https://github.com/{repo}.git\n"
            f"mkdir -p {RUNS_DIR}\n"
            # Template images differ (codex: Debian python without pip/venv;
            # dsh: Nix python). uv is a static binary that builds a working
            # venv on either, so prefer it and fall back to stdlib venv.
            # Repo deps are installed per test run by sync_deps().
            f"export UV_CACHE_DIR={UV_DIR}/cache\n"
            f"if [ ! -x {UV} ]; then curl -LsSf https://astral.sh/uv/install.sh "
            f"| env UV_INSTALL_DIR={UV_DIR}/bin UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 "
            f"|| true; fi\n"
            f"if [ ! -f {VENV_OK} ]; then rm -rf {VENV}; "
            f"if [ -x {UV} ]; then {UV} venv {VENV} -q; else python3 -m venv {VENV}; fi; "
            f"touch {VENV_OK}; fi\n"
        )
        res = await self.exec(script)
        if res.exit_code != 0:
            raise RuntimeError(f"sandbox provisioning failed: {res.combined[:2000]}")

    async def exec(self, command, timeout=None) -> ExecResult:
        t = self._s.sandbox_exec_timeout_seconds if timeout is None else timeout
        r = await self._client.post(f"/api/agents/{self.agent_id}/exec",
                                    headers=self._headers(),
                                    json={"command": command, "timeout": t})
        r.raise_for_status()
        d = r.json()
        return ExecResult(d["exitCode"], d.get("stdout", ""), d.get("stderr", ""))

    async def run_long(self, command: str, timeout=None) -> ExecResult:
        """Background-launch + poll: beats Maritime's 120s exec cap (spec §7)."""
        timeout = self._s.sandbox_run_timeout_seconds if timeout is None else timeout
        run_id = f"{int(time.time() * 1000)}-{self.task_id}"
        log, codef = f"{RUNS_DIR}/{run_id}.log", f"{RUNS_DIR}/{run_id}.code"
        # PATH instead of `source activate`: a missing activate script used to
        # fail silently and run tests on the system python.
        script = (f"export PATH={VENV}/bin:$PATH VIRTUAL_ENV={VENV}; "
                  f"{command}; echo $? > {codef}")
        # shlex.quote: a raw '...' wrap broke on commands with their own single
        # quotes (git commit -m '...'), so the code file never appeared.
        launch = (f"cd {WORKSPACE} && mkdir -p {RUNS_DIR} && nohup bash -c "
                  f"{shlex.quote(script)} > {log} 2>&1 & echo $!")
        started = await self.exec(launch, timeout=30)
        if started.exit_code != 0 or not started.stdout.strip():
            raise RuntimeError(f"run_long launch failed: {started.combined[:1000]}")
        deadline = time.monotonic() + timeout
        while True:
            poll = await self.exec(f"if [ -f {codef} ]; then echo DONE; cat {codef}; "
                                   f"else echo RUNNING; fi", timeout=15)
            if "DONE" in poll.stdout:
                lines = [ln.strip() for ln in poll.stdout.strip().splitlines() if ln.strip()]
                code = lines[-1] if lines else ""
                if not code.isdigit():
                    # code file exists but the exit code has not landed yet - keep polling
                    await asyncio.sleep(self._s.sandbox_poll_interval_seconds)
                    continue
                exit_code = int(code)
                out = await self.exec(f"tail -c 256000 {log}", timeout=30)
                return ExecResult(exit_code, out.stdout, out.stderr)
            if time.monotonic() > deadline:
                return ExecResult(124, f"run_long timeout after {timeout}s (log: {log})", "")
            await asyncio.sleep(self._s.sandbox_poll_interval_seconds)

    async def sync_deps(self) -> ExecResult:
        """Install repo deps into the venv before each test run.

        The harness may add requirements files (or the repo may have none), so
        re-sync every run and guarantee pytest is present for the default test
        command. No `exit` here: run_long needs the trailing `echo $?` to run.
        """
        script = (
            f"export UV_CACHE_DIR={UV_DIR}/cache; "
            f"if [ -x {UV} ]; then PIP=\"{UV} pip install -q --python {VENV}/bin/python\"; "
            f"else PIP=\"{VENV}/bin/python -m pip install -q\"; fi; "
            f"rc=0; "
            f"for f in requirements.txt requirements-dev.txt; do "
            f"if [ -f \"$f\" ]; then $PIP -r \"$f\" || rc=1; fi; done; "
            # Best-effort: many pyproject.toml files only configure tools.
            f"if [ -f pyproject.toml ] || [ -f setup.py ]; then $PIP -e . >/dev/null 2>&1 "
            f"|| echo \"note: editable install of project skipped\"; fi; "
            f"{VENV}/bin/python -m pytest --version >/dev/null 2>&1 || $PIP pytest || rc=1; "
            f"test $rc -eq 0"
        )
        return await self.run_long(script)

    async def read_file(self, path: str) -> str:
        r = await self._client.get(f"/api/agents/{self.agent_id}/files/download",
                                   headers=self._headers(), params={"path": path})
        r.raise_for_status()
        return r.text

    async def write_file(self, path: str, content: str) -> None:
        r = await self._client.put(f"/api/agents/{self.agent_id}/files/write",
                                   headers=self._headers(),
                                   json={"path": path, "content": content})
        r.raise_for_status()

    async def list_files(self, path: str) -> list[dict]:
        r = await self._client.get(f"/api/agents/{self.agent_id}/files/list",
                                   headers=self._headers(), params={"path": path})
        r.raise_for_status()
        return r.json().get("entries", [])

    async def diff(self) -> str:
        """Cumulative staged diff against the base branch (no in-VM commits)."""
        res = await self.exec(f"cd {WORKSPACE} && git add -A && git diff --cached {self.base_branch}")
        return res.stdout

    async def sleep(self) -> None:
        await self._client.post(f"/api/agents/{self.agent_id}/sleep", headers=self._headers())


def make_sandbox(settings, task_id: str, repo: str, base_branch: str,
                 client=None, agent_id: str | None = None) -> MaritimeSandbox:
    client = client or httpx.AsyncClient(base_url=settings.maritime_base_url, timeout=130)
    sb = MaritimeSandbox(settings, client, task_id, repo, base_branch)
    if agent_id:
        sb.agent_id = agent_id
    return sb
