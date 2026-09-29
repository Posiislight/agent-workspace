import { useEffect, useState } from "react";
import type { PipelineEvent } from "../api/types";
import { fmtDuration } from "./ReplayBar";

const t = (iso: string) => new Date(iso).getTime();

/**
 * The approval gate's headline: the task's VM sleeps while a human decides
 * (no awake-minutes accrue), then wakes and picks up where it left off.
 *
 * `pausedAt` set and no wake yet → asleep now. A `sleep_wake` event → show how
 * long it slept and how quickly work restarted (sleep_wake → next node event,
 * which covers waking the VM).
 */
export default function SleepWakeCard({
  events,
  pausedAt,
  asleep,
  vmMinutes,
}: {
  events: PipelineEvent[];
  pausedAt: string | null;
  asleep: boolean;
  vmMinutes?: number;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!asleep) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [asleep]);

  const wakeIdx = events.map((e) => e.type).lastIndexOf("sleep_wake");
  const wake = wakeIdx >= 0 ? events[wakeIdx] : null;
  if (!asleep && !wake) return null;

  const d = (wake?.data ?? {}) as { paused_at?: string; resumed_at?: string; decision?: string };
  const sleptFrom = d.paused_at ?? pausedAt;
  const slept = wake
    ? sleptFrom && d.resumed_at ? t(d.resumed_at) - t(sleptFrom) : null
    : sleptFrom ? now - t(sleptFrom) : null;
  const backAt = wake
    ? events.slice(wakeIdx + 1).find((e) => e.type === "node_started" || e.type === "tool_call")
    : undefined;
  const wakeLatency = wake && backAt ? t(backAt.ts) - t(wake.ts) : null;

  // Awake share: VM awake-minutes against the run's wall-clock span.
  const span = events.length > 1 ? t(events[events.length - 1].ts) - t(events[0].ts) : 0;
  const wall = asleep ? now - t(events[0]?.ts ?? now) : span;
  const awakeShare = vmMinutes && wall > 0 ? Math.min(1, (vmMinutes * 60000) / wall) : null;

  return (
    <section className={`card sleep-card ${asleep ? "is-asleep" : "is-awake"}`}>
      <div className="sleep-head">
        <span className="sleep-icon" aria-hidden>{asleep ? "☾" : "☀"}</span>
        <div>
          <h3>{asleep ? "VM asleep at the approval gate" : "VM woke up and resumed"}</h3>
          <p className="muted">
            {asleep
              ? "No awake-minutes accrue while the task waits for a human. Approving or rejecting wakes the same VM, with the repo, dependencies and agent session intact."
              : `Resumed on ${d.decision ?? "a decision"}: the same VM picked up from the checkpoint instead of rebuilding its workspace.`}
          </p>
        </div>
      </div>
      <dl className="sleep-stats">
        {slept !== null && (
          <div>
            <dt>{asleep ? "Asleep for" : "Slept for"}</dt>
            <dd className="mono">{fmtDuration(slept)}</dd>
          </div>
        )}
        {wakeLatency !== null && (
          <div>
            <dt>Back to work in</dt>
            <dd className="mono">{(wakeLatency / 1000).toFixed(1)}s</dd>
          </div>
        )}
        {awakeShare !== null && (
          <div>
            <dt>VM billed</dt>
            <dd className="mono">
              {vmMinutes!.toFixed(1)} min <span className="muted">of {fmtDuration(wall)}</span>
            </dd>
          </div>
        )}
        {awakeShare !== null && (
          <div>
            <dt>Idle time not billed</dt>
            <dd className="mono">{Math.round((1 - awakeShare) * 100)}%</dd>
          </div>
        )}
      </dl>
    </section>
  );
}
