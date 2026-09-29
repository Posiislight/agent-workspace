import { STAGES, STATUS_TO_STAGE } from "../api/types";
import type { StageId, StageState, TaskState } from "../api/types";

const NODE_TO_STAGE: Record<string, StageId> = {
  coding_agent: "coding",
  tester: "tester",
  reviewer: "reviewer",
  commit_pr: "commit_pr",
};

// needs_human is reached from several nodes; error_log entries are prefixed
// with the node that gave up (e.g. "tester: retry bound exceeded ...").
export function stuckStage(task: TaskState): StageId {
  for (const entry of [...(task.error_log ?? [])].reverse()) {
    const stage = NODE_TO_STAGE[entry.split(":", 1)[0]];
    if (stage) return stage;
  }
  return "human_approval";
}

export function deriveStageStates(task: TaskState): Record<StageId, StageState> {
  const states = { planner: "done", researcher: "done" } as Record<StageId, StageState>;
  for (const s of STAGES) states[s.id] = "pending";
  const current =
    task.status === "needs_human" ? stuckStage(task) : STATUS_TO_STAGE[task.status] ?? null;
  const currentIdx = current ? STAGES.findIndex((s) => s.id === current) : -1;
  for (let i = 0; i < STAGES.length; i++) {
    if (i < currentIdx) states[STAGES[i].id] = "done";
  }
  if (current) {
    if (task.status === "failed" || task.status === "needs_human") states[current] = "failed";
    else if (task.status === "done") states[current] = "done";
    else states[current] = "running";
  }
  return states;
}
