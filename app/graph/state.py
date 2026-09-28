from dataclasses import asdict, dataclass, field
from typing import Literal, Optional, TypedDict

TaskStatus = Literal[
    "planning", "researching", "coding", "testing", "reviewing",
    "awaiting_approval", "committing", "done", "failed", "needs_human",
]


@dataclass
class TestResult:
    passed: bool
    failing_output: str = ""
    failing_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TestResult":
        return cls(passed=d["passed"], failing_output=d.get("failing_output", ""),
                   failing_tests=d.get("failing_tests", []))


class TaskState(TypedDict):
    task_id: str
    task_description: str
    repo: str
    base_branch: str
    test_command: str
    plan: Optional[str]
    research_notes: Optional[str]
    code_diff: Optional[str]
    test_results: Optional[dict]
    review_comments: Optional[list[str]]
    approval_status: Literal["pending", "approved", "rejected", "needs_changes"]
    retry_counts: dict
    cost_so_far: float
    sandbox_id: Optional[str]
    computer_id: Optional[str]
    status: TaskStatus
    error_log: list
    paused_at: Optional[str]
    resumed_at: Optional[str]
    pr_url: Optional[str]
    pr_number: Optional[int]
    agent_id: Optional[str]
    template_id: Optional[str]


def initial_state(task_id: str, description: str, repo: str, base_branch: str,
                  test_command: str, template_id: str = "codex") -> TaskState:
    return TaskState(
        task_id=task_id, task_description=description, repo=repo, base_branch=base_branch,
        test_command=test_command or "", plan=None, research_notes=None, code_diff=None,
        test_results=None, review_comments=None, approval_status="pending",
        retry_counts={"testing": 0, "coding": 0}, cost_so_far=0.0, sandbox_id=None,
        computer_id=None, status="planning", error_log=[],
        paused_at=None, resumed_at=None, pr_url=None, pr_number=None,
        agent_id=None, template_id=template_id,
    )
