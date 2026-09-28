"""Coding harnesses (spec phase-4 §5).

`openrouter` is the in-process JSON-ops loop (app.graph.nodes.coding_agent).
`dsh` / `codex` are Maritime templates whose CLI runs headless inside the task
workspace through `run_long`; the result is read back as a git diff, so the
Tester / push / PR path is harness-agnostic.
"""
import shlex
import time

from app.sandbox.base import ExecResult

HARNESSES = ("openrouter", "dsh", "codex")
CLI_HARNESSES = ("dsh", "codex")

CLI_PREAMBLE = """You are running headless inside a git checkout of the repository (the current
directory). Implement the task below by editing files in place. Make the smallest correct
change that satisfies the plan and keep the existing test suite passing. Do NOT run
git commit, git push, or open a pull request - the orchestrator does that. When you are
done, stop.

"""


def validate_harness(name: str) -> str:
    if name not in HARNESSES:
        raise ValueError(f"unknown harness {name!r}; expected one of {HARNESSES}")
    return name


def harness_command(settings, harness: str, brief_path: str) -> str:
    template = {"dsh": settings.harness_cmd_dsh, "codex": settings.harness_cmd_codex}[harness]
    return template.replace("{brief}", shlex.quote(brief_path))


async def run_cli_harness(settings, sb, harness: str, brief: str,
                          timeout: int | None = None) -> tuple[ExecResult, str]:
    """Write the brief into the VM and run the harness CLI in the workspace."""
    path = f"/data/.runs/brief-{int(time.time() * 1000)}.md"
    await sb.write_file(path, CLI_PREAMBLE + brief)
    cmd = harness_command(settings, harness, path)
    res = await sb.run_long(cmd, timeout=timeout)
    return res, cmd
