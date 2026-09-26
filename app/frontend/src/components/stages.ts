import { STAGES, STATUS_TO_STAGE } from "../api/types";
import type { StageId, StageState, TaskState } from "../api/types";

export function deriveStageStates(task: TaskState): Record<StageId, StageState> {
  const states = Object.fromEntries(STAGES.map((s) => [s.id, "pending" as StageState])) as Record<
    StageId,
    StageState
  >;
  const current = STATUS_TO_STAGE[task.status] ?? null;
  const currentIdx = current ? STAGES.findIndex((s) => s.id === current) : -1;
  for (let i = 0; i < STAGES.length; i++) {
    if (i < currentIdx) states[STAGES[i].id] = "done";
  }
  if (current) {
    if (task.status === "failed") states[current] = "failed";
    else if (task.status === "done") states[current] = "done";
    else states[current] = "running";
  }
  if (task.status === "failed" && currentIdx === -1) {
    // couldn't map; nothing to mark
  }
  return states;
}
