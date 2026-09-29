import { STAGES } from "../api/types";
import type { StageId, StageState } from "../api/types";

// Which retry counter belongs to which stage: review-driven retries re-run
// coding, test-driven retries re-run testing. Maxima mirror app/graph/edges.py.
const RETRY_KEY: Partial<Record<StageId, { key: string; max: number }>> = {
  coding: { key: "coding", max: 2 },
  tester: { key: "testing", max: 3 },
};

export default function StageTimeline({
  states,
  retryCounts,
  stages = STAGES,
}: {
  states: Record<StageId, StageState>;
  retryCounts?: Record<string, number>;
  stages?: { id: StageId; label: string }[];
}) {
  return (
    <ol className="timeline">
      {stages.map((s, i) => {
        const r = RETRY_KEY[s.id];
        const n = r ? retryCounts?.[r.key] ?? 0 : 0;
        return (
          <li key={s.id} className={`stage stage-${states[s.id]}`} title={`${s.label}: ${states[s.id]}`}>
            <span className="stage-marker" aria-hidden>
              {states[s.id] === "done" ? "✓" : states[s.id] === "failed" ? "!" : i + 1}
            </span>
            <div className="stage-body">
              <span className="stage-label">{s.label}</span>
              {states[s.id] === "running" && <span className="stage-state">running</span>}
              {states[s.id] === "failed" && <span className="stage-state">stopped</span>}
              {r && n > 0 && (
                <span
                  className={`retry ${n >= r.max ? "maxed" : ""}`}
                  title={`${n} of ${r.max} ${r.key} retries used`}
                >
                  ↻ {n}/{r.max}
                </span>
              )}
            </div>
            {i < stages.length - 1 && <span className="stage-line" />}
          </li>
        );
      })}
    </ol>
  );
}
