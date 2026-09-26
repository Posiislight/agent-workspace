import { STAGES } from "../api/types";
import type { StageId, StageState } from "../api/types";

export default function StageTimeline({
  states,
}: {
  states: Record<StageId, StageState>;
}) {
  return (
    <ol className="timeline">
      {STAGES.map((s, i) => (
        <li key={s.id} className={`stage stage-${states[s.id]}`}>
          <span className="stage-marker" />
          <div className="stage-body">
            <span className="stage-label">{s.label}</span>
            <span className={`chip stage-chip-${states[s.id]}`}>{states[s.id]}</span>
          </div>
          {i < STAGES.length - 1 && <span className="stage-line" />}
        </li>
      ))}
    </ol>
  );
}
