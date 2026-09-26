export type TaskStatus =
  | "planning"
  | "researching"
  | "coding"
  | "testing"
  | "reviewing"
  | "awaiting_approval"
  | "committing"
  | "done"
  | "failed"
  | "needs_human";

export type StageId = "planner" | "researcher" | "coding" | "tester" | "reviewer" | "human_approval" | "commit_pr";

export type StageState = "pending" | "running" | "done" | "failed";

export interface TaskCreateRequest {
  task_description: string;
  repo: string;
  base_branch: string;
  test_command?: string | null;
  model_overrides?: Record<string, string>;
}

export interface TaskCreateResponse {
  task_id: string;
}

export interface TestResult {
  passed: boolean;
  failing_output: string;
  failing_tests: string[];
}

export interface TaskState {
  task_id: string;
  task_description: string;
  repo: string;
  base_branch: string;
  test_command: string;
  plan: string | null;
  research_notes: string | null;
  code_diff: string | null;
  test_results: TestResult | null;
  review_comments: string[] | null;
  approval_status: "pending" | "approved" | "rejected" | "needs_changes";
  retry_counts: Record<string, number>;
  cost_so_far: number;
  status: TaskStatus;
  error_log: string[];
  model_overrides: Record<string, string>;
  paused_at: string | null;
  resumed_at: string | null;
  pr_url: string | null;
}

export interface TaskSummary {
  task_id: string;
  repo: string;
  task_description: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  cost_so_far: number | null;
  pr_url: string | null;
}

export interface Artifacts {
  plan: string | null;
  research_notes: string | null;
  code_diff: string | null;
  test_results: TestResult | null;
  review_comments: string[] | null;
}

export interface PipelineEvent {
  task_id: string;
  node: string;
  type: string;
  data: Record<string, unknown>;
  ts: string;
}

export const STAGES: { id: StageId; label: string }[] = [
  { id: "planner", label: "Planner" },
  { id: "researcher", label: "Researcher" },
  { id: "coding", label: "Coding" },
  { id: "tester", label: "Tester" },
  { id: "reviewer", label: "Reviewer" },
  { id: "human_approval", label: "Approval" },
  { id: "commit_pr", label: "Commit & PR" },
];

export const STATUS_TO_STAGE: Partial<Record<TaskStatus, StageId>> = {
  planning: "planner",
  researching: "researcher",
  coding: "coding",
  testing: "tester",
  reviewing: "reviewer",
  awaiting_approval: "human_approval",
  committing: "commit_pr",
  done: "commit_pr",
  needs_human: "human_approval",
};
