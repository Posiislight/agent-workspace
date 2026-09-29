import { SPEEDS } from "./replay";
import type { Replay } from "./replay";

export function fmtDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export default function ReplayBar({ replay, onExit }: { replay: Replay; onExit: () => void }) {
  const { cursor, total, events, firstTs, playing, complete, speed } = replay;
  const last = events[events.length - 1];
  const runClock = firstTs && last ? new Date(last.ts).getTime() - new Date(firstTs).getTime() : 0;
  const pct = total ? (cursor / total) * 100 : 0;

  return (
    <section className="replay-bar card" aria-label="Run replay">
      <div className="replay-row">
        <span className="replay-badge">Replay</span>
        <button className="btn" onClick={replay.toggle} disabled={!replay.loaded || total === 0}>
          {complete ? "↻ Replay again" : playing ? "❚❚ Pause" : "▶ Play"}
        </button>
        <div className="segmented" role="group" aria-label="Replay speed">
          {SPEEDS.map((s) => (
            <button
              key={s}
              className={`seg${speed === s ? " active" : ""}`}
              aria-pressed={speed === s}
              onClick={() => replay.setSpeed(s)}
            >
              {s}×
            </button>
          ))}
        </div>
        {!complete && (
          <button className="btn ghost" onClick={replay.skipToEnd} disabled={!replay.loaded}>
            Skip to end
          </button>
        )}
        <span className="replay-clock mono">
          {last ? (
            <>
              run clock <strong>+{fmtDuration(runClock)}</strong>
              <span className="muted"> · {new Date(last.ts).toLocaleString()}</span>
            </>
          ) : replay.error ? (
            <span className="err-text">Couldn't load the recording: {replay.error}</span>
          ) : replay.loaded && total === 0 ? (
            <span className="muted">No recorded events for this task.</span>
          ) : (
            <span className="muted">Loading recording…</span>
          )}
        </span>
        <button className="btn ghost" onClick={onExit}>
          Exit replay
        </button>
      </div>
      <div
        className="replay-progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={cursor}
        aria-label={`${cursor} of ${total} events`}
      >
        <span style={{ width: `${pct}%` }} />
      </div>
    </section>
  );
}
