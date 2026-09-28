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
        self.deps_synced = 0
        self.sync_result = ExecResult(0, "", "")
        self.slept = False

    async def ensure(self) -> str:
        self.ensured += 1
        return self.agent_id

    async def exec(self, command, timeout=None) -> ExecResult:
        self.shells.append(command)
        return ExecResult(0, self.exec_stdout, "")

    async def sync_deps(self) -> ExecResult:
        self.deps_synced += 1
        return self.sync_result

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
        return ExecResult(0, "", "")

    async def open_url(self, computer_id, url):
        self.opened.append(url)
        return SimpleNamespace(computer_id=computer_id, frame_id=2,
                               width=1200, height=750, raw={})

    async def viewer_link(self, computer_id, mode="watch", ttl_s=600) -> str:
        self.viewer_calls += 1
        return self.viewer_url

    async def sleep(self, computer_id) -> None:
        pass


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

    async def incrbyfloat(self, key, amount):
        current = float(self.store.get(key, 0.0) or 0.0)
        current += float(amount)
        self.store[key] = repr(current)
        return current


class StubAgent:
    """FIFO-scripted MaritimeAgentClient replacement. Records calls."""

    def __init__(self, responses=None, agent_id="agent-1", compute_seconds=120.0):
        self.agent_id = agent_id
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.created: list[str] = []   # template ids
        self.waited: list[str] = []
        self.slept: list[str] = []
        self.compute_seconds = compute_seconds

    async def create(self, name, template_id):
        self.created.append(template_id)
        return self.agent_id

    async def wait_active(self, agent_id, timeout_s=180, poll_s=5):
        self.waited.append(agent_id)
        return agent_id

    async def chat(self, agent_id, message, conversation_id, timeout_s=None):
        self.calls.append({"agent_id": agent_id, "message": message,
                           "conversation_id": conversation_id})
        if not self.responses:
            raise AssertionError("StubAgent exhausted — add scripted responses")
        return self.responses.pop(0)

    async def sleep(self, agent_id):
        self.slept.append(agent_id)

    async def total_compute_seconds(self, agent_id):
        return self.compute_seconds

    async def llm_status(self, agent_id):
        return {"has_key": True, "using_maritime": True}


def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None,
                  github=None, cost=None, agent=None) -> SimpleNamespace:
    """Build a Services namespace wired to fakes. sandbox_factory returns the shared stub."""
    from app.graph.nodes.helpers import Services
    from app.services.cost_tracker import CostTracker

    settings = settings or Settings()
    sandbox = sandbox or StubSandbox()
    if cost is None and redis is not None:
        cost = CostTracker(redis, settings.vm_cost_per_hour)
    return Services(
        settings=settings,
        llm=llm or StubLLM([]),
        prices=prices or StubPriceTable(),
        publisher=publisher or FakePublisher(),
        sandbox_factory=lambda state: sandbox,
        computers=computers or StubComputers(),
        redis=redis,
        pg_dsn=pg_dsn,
        github=github,
        cost=cost,
        agent=agent or StubAgent(),
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
        self.updated_bodies: list[tuple[int, str]] = []

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

    async def update_pr_body(self, repo, number, body):
        self.updated_bodies.append((number, body))
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

    async def aclose(self):
        pass