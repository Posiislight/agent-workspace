from types import SimpleNamespace

from app.config import Settings
from app.graph.nodes.helpers import Services
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
                 diff_text="diff --git a/app.py b/app.py\n+def add(a, b):\n    return a + b\n"):
        self.agent_id = "stub-agent"
        self.run_results = list(run_results or [])
        self.default_run_result = ExecResult(0, "1 passed in 0.01s", "")
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


def make_services(llm=None, sandbox=None, publisher=None, prices=None,
                  computers=None, settings=None, redis=None, pg_dsn=None) -> SimpleNamespace:
    """Build a Services namespace wired to fakes. sandbox_factory returns the shared stub."""
    from app.graph.nodes.helpers import Services

    settings = settings or Settings()
    sandbox = sandbox or StubSandbox()
    return Services(
        settings=settings,
        llm=llm or StubLLM([]),
        prices=prices or StubPriceTable(),
        publisher=publisher or FakePublisher(),
        sandbox_factory=lambda state: sandbox,
        computers=computers or StubComputers(),
        redis=redis,
        pg_dsn=pg_dsn,
    )