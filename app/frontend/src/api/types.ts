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

export type StageId =
  | "planner"
  | "researcher"
  | "coding"
  | "tester"
  | "reviewer"
  | "human_approval"
  | "commit_pr";

export type StageState = "pending" | "running" | "done" | "failed";

export interface TaskCreateRequest {
  task_description: string;
  repo: string;
  base_branch: string;
  test_command?: string | null;
  template_id: string;
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
  template_id?: string;
  agent_id?: string | null;
  paused_at: string | null;
  resumed_at: string | null;
  pr_url: string | null;
  vm_cost?: number;
  vm_minutes?: number;
  llm_cost?: number;
}

export interface TaskSummary {
  task_id: string;
  repo: string;
  task_description: string | null;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  cost_so_far: number | null;
  vm_cost?: number;
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
  /** Redis stream entry id (SSE `id:`); used to drop replays after reconnect. */
  id?: string;
}

export const STAGES: { id: StageId; label: string }[] = [
  { id: "coding", label: "Coding" },
  { id: "tester", label: "Tester" },
  { id: "reviewer", label: "Reviewer" },
  { id: "human_approval", label: "Approval" },
  { id: "commit_pr", label: "Commit & PR" },
];

// Runs recorded before the graph started at coding also went through these;
// replay shows them in front of STAGES when the recording contains them.
export const LEGACY_STAGES: { id: StageId; label: string }[] = [
  { id: "planner", label: "Planner" },
  { id: "researcher", label: "Researcher" },
];

export const STATUS_TO_STAGE: Partial<Record<TaskStatus, StageId>> = {
  // Legacy statuses from before the graph started at coding.
  planning: "coding",
  researching: "coding",
  coding: "coding",
  testing: "tester",
  reviewing: "reviewer",
  awaiting_approval: "human_approval",
  committing: "commit_pr",
  done: "commit_pr",
  needs_human: "human_approval",
};
