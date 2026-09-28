from types import SimpleNamespace

from app.config import Settings
from app.llm.openrouter import LLMResult
from app.sandbox.base import ExecResult


class StubLLM:
    """Scripted FIFO LLM. Records every call for assertions."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, model, messages, stream_cb=None):
        self.calls.append({"model": model, "messages": messages})
        if not self.responses:
            raise AssertionError("StubLLM exhausted — add scripted responses")
        return LLMResult(self.responses.pop(0), 10, 5, model)


class StubPriceTable:
    def cost(self, model, prompt_tokens, completion_tokens) -> float:
        return 0.001


class FakePublisher:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)
        return "0-1"


class StubSandbox:
    def __init__(self, run_results=None, exec_stdout="ok",
                 diff_text="diff --git a/app.py b/app.py\n+def add(a, b):\n    return a + b\n",
                 default_run_result=None):
        self.agent_id = "stub-agent"
        self.run_results = list(run_results or [])
        self.default_run_result = default_run_result or ExecResult(0, "1 passed in 0.01s", "")
        self.exec_stdout = exec_stdout
        self.diff_text = diff_text
        self.files: dict[str, str] = {}
        self.written: list[tuple[str, str]] = []
        self.shells: list[str] = []
        self.run_calls: list[str] = []
        self.ensured = 0
        self.slept = False

    async def ensure(self) -> str:
        self.ensured += 1
        return self.agent_id

    async def exec(self, command, timeout=None) -> ExecResult:
        self.shells.append(command)
        return ExecResult(0, self.exec_stdout, "")

    async def run_long(self, command: str, timeout=None) -> ExecResult:
        self.run_calls.append(command)
        if self.run_results:
            return self.run_results.pop(0)
        return self.default_run_result

    async def read_file(self, path: str) -> str:
        return self.files.get(path, "")

    async def write_file(self, path: str, content: str) -> None:
        self.written.append((path, content))
        self.files[path] = content

    async def list_files(self, path: str) -> list[dict]:
        return []

    async def diff(self) -> str:
        return self.diff_text

    async def sleep(self) -> None:
        self.slept = True


class StubComputers:
    def __init__(self):
        self.computer_id = "comp-1"
        self.viewer_url = "https://view.maritime.sh/aw"
        self.opened: list[str] = []
        self.viewer_calls = 0

    async def ensure(self, task_id: str):
        return SimpleNamespace(computer_id=self.computer_id, frame_id=0,
                               width=1200, height=750, raw={})

    async def act(self, computer_id, action):
        return SimpleNamespace(computer_id=computer_id, frame_id=1,
                               width=1200, height=750, raw={})

    async def shell(self, computer_id, command, timeout_s=30) -> ExecResult:
        self.shells = getattr(self, "shells", [])
        self.shells.append(command)
        if "curl -s -o /dev/null" in command:
            return ExecResult(0, "UP", "")
        return ExecResult(0, "PREPARED", "")

    async def screenshot(self, computer_id):
        self.shots = getattr(self, "shots", 0) + 1
        return b"\x89PNG-fake-" + str(self.shots).encode(), "7"

    async def open_url(self, computer_id, url):
        self.opened.append(url)
        return SimpleNamespace(computer_id=computer_id, frame_id=2,
                               width=1200, height=750, raw={})

    async def viewer_link(self, computer_id, mode="watch", ttl_s=600) -> str:
        self.viewer_calls += 1
        return self.viewer_url

    async def sleep(self, computer_id) -> None:
        self.slept = getattr(self, "slept", 0) + 1


class FakeRedis:
    """Dict-backed async redis stub with the set/get/delete surface RunManager uses."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return self.store.pop(key, None) is not None


def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None,
                  github=None, sandbox_factory=None, pool=None,
                  workspaces=None) -> SimpleNamespace:
    """Build a Services namespace wired to fakes. sandbox_factory returns the shared stub."""
    from app.graph.nodes.helpers import Services

    settings = settings or Settings()
    sandbox = sandbox or StubSandbox()
    return Services(
        settings=settings,
        llm=llm or StubLLM([]),
        prices=prices or StubPriceTable(),
        publisher=publisher or FakePublisher(),
        sandbox_factory=sandbox_factory or (lambda state: sandbox),
        computers=computers or StubComputers(),
        redis=redis,
        pg_dsn=pg_dsn,
        github=github,
        pool=pool,
        workspaces=workspaces,
    )


class StubGitHub:
    """Fake GitHubClient that records calls for assertions."""

    def __init__(self, existing_prs=None, created=None, initial_comments=None):
        self.existing_prs = dict(existing_prs or [])
        self.created = created or {"number": 11,
                                   "html_url": "https://github.com/org/repo/pull/11",
                                   "draft": True}
        self.created_prs: list[dict] = []
        self.all_comments: list[dict] = [
            {"body": c["body"]} for c in (initial_comments or [])]
        self.comments: list[tuple[int, str]] = []
        self.ready: list[int] = []
        self.merged: list[int] = []

    async def find_open_pr(self, repo, head):
        return self.existing_prs.get(head)

    async def create_pr(self, repo, *, title, body, head, base, draft=True):
        self.created_prs.append({"title": title, "body": body, "head": head,
                                 "base": base, "draft": draft})
        owner = repo.split("/")[0]
        self.existing_prs[f"{owner}:{head}"] = dict(self.created)
        return self.created

    async def mark_ready(self, repo, number):
        self.ready.append(number)
        return {}

    async def list_comments(self, repo, number):
        return list(self.all_comments)

    async def add_comment(self, repo, number, body):
        self.comments.append((number, body))
        self.all_comments.append({"body": body})
        return {}

    async def merge_pr(self, repo, number):
        self.merged.append(number)
        return {}

    async def update_pr(self, repo, number, **fields):
        self.updated = getattr(self, "updated", [])
        self.updated.append((number, fields))
        return {}

    async def put_file(self, repo, path, content_b64, *, branch, message):
        self.files = getattr(self, "files", {})
        self.files[(branch, path)] = content_b64
        return {}

    async def job_log_tail(self, repo, job_id, limit=8000):
        return f"log for job {job_id}: AssertionError in test_add"

    async def aclose(self):
        pass

class FakeAgent:
    """MaritimeSandbox stand-in for TaskWorkspace tests: records scripts, tracks
    how many agents are awake at once across a shared counter."""

    def __init__(self, repo="org/repo", base_branch="main", awake_counter=None,
                 run_result=None, diff_text="diff --git a/f b/f\n+x\n", work_delay=0.0):
        self.repo = repo
        self.base_branch = base_branch
        self.agent_id = None
        self.last_created = False
        self.active_task = None
        self.execs: list[str] = []
        self.run_calls: list[str] = []
        self.slept = 0
        self.awake = False
        self.counter = awake_counter if awake_counter is not None else {"now": 0, "max": 0}
        self.run_result = run_result or ExecResult(0, "1 passed", "")
        self.diff_text = diff_text
        self.work_delay = work_delay
        self.files: dict[str, str] = {}

    def _wake(self):
        if not self.awake:
            self.awake = True
            self.counter["now"] += 1
            self.counter["max"] = max(self.counter["max"], self.counter["now"])

    async def ensure(self):
        self._wake()
        self.last_created = self.agent_id is None
        self.agent_id = self.agent_id or f"agent-{id(self)}"
        return self.agent_id

    async def exec(self, command, timeout=None):
        import asyncio
        self._wake()
        self.execs.append(command)
        if "git diff --cached --shortstat" in command:
            return ExecResult(0, " 1 file changed, 3 insertions(+), 1 deletion(-)", "")
        if "git diff --cached" in command:
            return ExecResult(0, self.diff_text, "")
        if "git checkout" in command:
            return ExecResult(0, "SWITCHED\n", "")
        if "memory.md" in command and "tail" in command:
            return ExecResult(0, "- earlier task | files: calc.py", "")
        await asyncio.sleep(0)
        return ExecResult(0, "ok", "")

    async def run_long(self, command, timeout=None):
        import asyncio
        self._wake()
        self.run_calls.append(command)
        if "sha256sum" in command:
            return ExecResult(0, "DEPS=installed" if self.slept == 0 else "DEPS=cached", "")
        if self.work_delay:
            await asyncio.sleep(self.work_delay)
        return self.run_result

    async def write_file(self, path, content):
        self.files[path] = content

    async def read_file(self, path):
        return self.files.get(path, "")

    async def list_files(self, path):
        return []

    async def sleep(self):
        if self.awake:
            self.awake = False
            self.counter["now"] -= 1
        self.slept += 1
