import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { LEGACY_STAGES, STAGES } from "../api/types";
import type { Artifacts, PipelineEvent, StageId, StageState, TaskState } from "../api/types";
import { deriveStageStates } from "./stages";

const NODE_TO_STAGE: Record<string, StageId> = {
  planner: "planner",
  researcher: "researcher",
  coding_agent: "coding",
  tester: "tester",
  reviewer: "reviewer",
  human_approval: "human_approval",
  commit_pr: "commit_pr",
};

// Long real gaps (an agent thinking for minutes, a VM asleep overnight) are
// capped so a replay plays in seconds; every event still gets a visible beat.
const MAX_GAP_MS = 2500;
const MIN_GAP_MS = 140;
export const SPEEDS = [1, 3, 10] as const;

export function replayStageList(events: PipelineEvent[]) {
  const legacy = events.some((e) => e.node === "planner" || e.node === "researcher");
  return legacy ? [...LEGACY_STAGES, ...STAGES] : STAGES;
}

/** Stage states as they stood right after `events` (a prefix of the recording). */
export function stagesFromEvents(
  events: PipelineEvent[],
  stages: { id: StageId }[],
): Record<StageId, StageState> {
  const order = stages.map((s) => s.id);
  const states = Object.fromEntries(order.map((id) => [id, "pending"])) as Record<StageId, StageState>;
  for (const ev of events) {
    const stage = NODE_TO_STAGE[ev.node];
    const idx = stage ? order.indexOf(stage) : -1;
    if (ev.node === "run_manager" && "error" in ev.data) {
      const running = order.find((id) => states[id] === "running");
      if (running) states[running] = "failed";
    }
    if (idx < 0) continue;
    if (ev.type === "node_started") {
      // A retry loops back to an earlier stage: later stages start over.
      order.forEach((id, i) => {
        if (i < idx) states[id] = "done";
        else if (i > idx) states[id] = "pending";
      });
      states[order[idx]] = "running";
    } else if (ev.type === "node_completed") {
      states[order[idx]] = "error" in ev.data ? "failed" : "done";
    } else if (ev.type === "sleep_wake") {
      states[order[idx]] = "done";
    }
  }
  return states;
}

function retryCountsFromEvents(events: PipelineEvent[]): Record<string, number> {
  const codingRuns = events.filter((e) => e.node === "coding_agent" && e.type === "node_started").length;
  const testFails = events.filter(
    (e) => e.node === "tester" && e.type === "node_completed" && e.data.passed === false,
  ).length;
  return { coding: Math.max(0, codingRuns - 1), testing: testFails };
}

/** Final artifacts, revealed only once the stage that produces each one has finished. */
function artifactsSoFar(task: TaskState, events: PipelineEvent[], complete: boolean): Artifacts {
  const finished = (node: string) =>
    complete || events.some((e) => e.node === node && e.type === "node_completed");
  return {
    plan: finished("planner") ? task.plan : null,
    research_notes: finished("researcher") ? task.research_notes : null,
    code_diff: finished("coding_agent") ? task.code_diff : null,
    test_results: finished("tester") ? task.test_results : null,
    review_comments: finished("reviewer") ? task.review_comments : null,
  };
}

function gapMs(prev: PipelineEvent | undefined, next: PipelineEvent, speed: number): number {
  if (!prev) return 400;
  const real = new Date(next.ts).getTime() - new Date(prev.ts).getTime();
  return Math.max(MIN_GAP_MS, Math.min(Number.isFinite(real) ? real : 0, MAX_GAP_MS)) / speed;
}

export function useReplay(taskId: string | undefined, task: TaskState | null, enabled: boolean) {
  const [all, setAll] = useState<PipelineEvent[] | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(true);
  const [speed, setSpeed] = useState<number>(3);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !taskId) return;
    setAll(null);
    setCursor(0);
    setPlaying(true);
    api.eventHistory(taskId).then(setAll, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, [enabled, taskId]);

  useEffect(() => {
    if (!enabled || !all || !playing) return;
    if (cursor >= all.length) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setCursor((c) => c + 1), gapMs(all[cursor - 1], all[cursor], speed));
    return () => clearTimeout(t);
  }, [enabled, all, cursor, playing, speed]);

  const events = useMemo(() => (all ? all.slice(0, cursor) : []), [all, cursor]);
  const complete = !!all && cursor >= all.length;
  const stages = useMemo(() => replayStageList(all ?? []), [all]);

  const states = useMemo(() => {
    const s = stagesFromEvents(events, stages);
    // The recording ends at the last node event; the final verdict (e.g. the
    // retry bound giving up) lives only in the task state.
    return complete && task ? { ...s, ...deriveStageStates(task) } : s;
  }, [events, stages, complete, task]);

  return {
    loaded: all !== null,
    error,
    total: all?.length ?? 0,
    cursor,
    events,
    complete,
    playing,
    speed,
    stages,
    states,
    retryCounts: complete && task ? task.retry_counts : retryCountsFromEvents(events),
    artifacts: task ? artifactsSoFar(task, events, complete) : null,
    firstTs: all?.[0]?.ts ?? null,
    setSpeed,
    toggle: () => {
      if (complete) setCursor(0);
      setPlaying((p) => !p || complete);
    },
    restart: () => {
      setCursor(0);
      setPlaying(true);
    },
    skipToEnd: () => {
      setCursor(all?.length ?? 0);
      setPlaying(false);
    },
  };
}

export type Replay = ReturnType<typeof useReplay>;
