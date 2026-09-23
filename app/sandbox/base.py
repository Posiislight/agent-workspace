from dataclasses import dataclass
from typing import Protocol


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        out = self.stdout.strip()
        if self.stderr.strip():
            out += "\n" + self.stderr.strip()
        return out


class Sandbox(Protocol):
    agent_id: str | None

    async def ensure(self) -> str: ...
    async def exec(self, command, timeout=None) -> ExecResult: ...
    async def run_long(self, command: str, timeout=None) -> ExecResult: ...
    async def read_file(self, path: str) -> str: ...
    async def write_file(self, path: str, content: str) -> None: ...
    async def list_files(self, path: str) -> list[dict]: ...
    async def diff(self) -> str: ...
    async def sleep(self) -> None: ...
