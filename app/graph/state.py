from dataclasses import asdict, dataclass, field
from typing import Literal, Optional, TypedDict

TaskStatus = Literal[
    "planning", "researching", "coding", "testing", "reviewing",
    "awaiting_approval", "awaiting_budget", "committing", "done", "failed", "needs_human",
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
    model_overrides: dict
    paused_at: Optional[str]
    resumed_at: Optional[str]
    pr_url: Optional[str]
    pr_number: Optional[int]
    # Phase 4/5
    harness: str                        # openrouter | dsh | codex
    workspace_slot: int                 # which (repo, template, slot) agent holds the work
    workspace_metrics: list             # cold/warm startup measurements
    candidates: list                    # Best-of-N harness list requested
    candidate_results: list             # Best-of-N scoreboard rows
    followup_request: Optional[dict]    # pending {source, instruction, at}
    followups: list                     # history of follow-up turns
    ci_fix_attempts: int
    ci_seen: list                       # "{sha}:{check}" already handled
    budget_usd: float                   # 0 = unlimited
    llm_cost: float
    vm_seconds: float
    vm_cost: float
    preview: Optional[dict]             # {command, port, path, setup}
    visual_proof: Optional[dict]


def initial_state(task_id: str, description: str, repo: str, base_branch: str,
                  test_command: str, model_overrides: dict, *, harness: str = "openrouter",
                  candidates: list | None = None, budget_usd: float = 0.0,
                  preview: dict | None = None) -> TaskState:
    return TaskState(
        task_id=task_id, task_description=description, repo=repo, base_branch=base_branch,
        test_command=test_command or "", plan=None, research_notes=None, code_diff=None,
        test_results=None, review_comments=None, approval_status="pending",
        retry_counts={"testing": 0, "coding": 0}, cost_so_far=0.0, sandbox_id=None,
        computer_id=None, status="planning", error_log=[], model_overrides=model_overrides,
        paused_at=None, resumed_at=None, pr_url=None, pr_number=None,
        harness=harness, workspace_slot=0, workspace_metrics=[],
        candidates=list(candidates or []), candidate_results=[], followup_request=None,
        followups=[], ci_fix_attempts=0, ci_seen=[], budget_usd=float(budget_usd or 0.0),
        llm_cost=0.0, vm_seconds=0.0, vm_cost=0.0, preview=preview, visual_proof=None,
    )
