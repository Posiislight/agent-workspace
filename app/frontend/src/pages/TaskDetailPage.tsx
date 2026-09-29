import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, subscribeToEvents, ApiError } from "../api/client";
import type { PipelineEvent, TaskState } from "../api/types";
import { deriveStageStates, stuckStage } from "../components/stages";
import { STAGES } from "../api/types";
import StageTimeline from "../components/StageTimeline";
import ApprovalPrompt from "../components/ApprovalPrompt";
import ArtifactsView from "../components/ArtifactsView";
import ReplayBar from "../components/ReplayBar";
import SleepWakeCard from "../components/SleepWakeCard";
import { useReplay } from "../components/replay";

type NotFound = "not-found";

function eventSummary(ev: PipelineEvent): string {
  const d = ev.data as Record<string, unknown>;
  if (typeof d.reply === "string") {
    const firstLine = d.reply.trim().split("\n").find((l) => l.trim()) ?? "";
    return (d.done === false ? "no DONE · " : "") + firstLine;
  }
  if (typeof d.command === "string") return `${d.command} → exit ${d.exit_code}`;
  if ("verdict" in d) return String(d.verdict);
  if ("passed" in d) return d.passed ? "passed" : "failed";
  return Object.keys(d).length ? JSON.stringify(d) : "";
}

function EventRow({ ev }: { ev: PipelineEvent }) {
  const d = ev.data as Record<string, unknown>;
  const hasData = Object.keys(d).length > 0;
  const head = (
    <>
      <span className="muted mono ts">{new Date(ev.ts).toLocaleTimeString()}</span>
      <span className="mono node">{ev.node}</span>
      <span className={`chip ev-${ev.type}`}>{ev.type.replace("_", " ")}</span>
      {hasData && <span className="ev-data">{eventSummary(ev)}</span>}
    </>
  );
  if (!hasData) return <li className="ev-row">{head}</li>;
  const { reply, ...rest } = d;
  return (
    <li>
      <details className="ev-details">
        <summary className="ev-row">{head}</summary>
        {typeof reply === "string" && <pre className="prose">{reply}</pre>}
        {Object.keys(rest).length > 0 && <pre>{JSON.stringify(rest, null, 2)}</pre>}
      </details>
    </li>
  );
}

function splitError(entry: string): { node: string; msg: string } {
  const m = entry.match(/^([a-z_]+):\s*(.*)$/s);
  return m ? { node: m[1], msg: m[2] } : { node: "", msg: entry };
}

export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const replaying = searchParams.has("replay");
  const [task, setTask] = useState<TaskState | NotFound | null>(null);
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [decideError, setDecideError] = useState<string | null>(null);
  const [restarting, setRestarting] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const refetch = useCallback(async () => {
    if (!taskId) return;
    try {
      setTask(await api.getTask(taskId));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setTask("not-found");
    }
  }, [taskId]);

  useEffect(() => {
    if (!taskId) return;
    setTask(null);
    setEvents([]);
    refetch();

    const seen = new Set<string>();
    // The stream replays every past event on connect; coalesce the resulting
    // refetches instead of firing one per event.
    let pending: ReturnType<typeof setTimeout> | null = null;
    const scheduleRefetch = () => {
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        refetch();
      }, 300);
    };
    const onEvent = (ev: PipelineEvent) => {
      if (ev.id) {
        if (seen.has(ev.id)) return; // replay after SSE reconnect
        seen.add(ev.id);
      }
      setEvents((prev) => {
        const next = [...prev, ev];
        return next.length > 500 ? next.slice(-500) : next;
      });
      scheduleRefetch();
    };
    const unsub = replaying ? () => {} : subscribeToEvents(taskId, onEvent, setConnected);
    return () => {
      if (pending) clearTimeout(pending);
      unsub();
    };
  }, [taskId, refetch, replaying]);

  useEffect(() => {
    if (task && task !== "not-found")
      document.title = `${(task.task_description ?? task.task_id).split("\n", 1)[0].slice(0, 60)} · agent-workspace`;
  }, [task]);


  const decide = async (decision: "approve" | "reject", feedback: string) => {
    if (!taskId) return;
    setDeciding(true);
    setDecideError(null);
    try {
      if (decision === "approve") await api.approve(taskId, feedback);
      else await api.reject(taskId, feedback);
    } catch (e) {
      setDecideError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeciding(false);
    }
  };

  const restart = async () => {
    if (!taskId) return;
    setRestarting(true);
    setDecideError(null);
    try {
      await api.restart(taskId);
    } catch (e) {
      setDecideError(e instanceof Error ? e.message : String(e));
    } finally {
      setRestarting(false);
    }
  };

  const replay = useReplay(taskId, task && task !== "not-found" ? task : null, replaying);
  const shownEvents = replaying ? replay.events : events;

  // Auto-scroll event log
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [shownEvents]);

  // Latest coding-agent reply, from its node_completed event.
  const codingReply = useMemo(() => {
    for (let i = shownEvents.length - 1; i >= 0; i--) {
      const ev = shownEvents[i];
      if (ev.node === "coding_agent" && typeof ev.data.reply === "string") return ev.data.reply;
    }
    return null;
  }, [shownEvents]);

  const setReplay = (on: boolean) =>
    setSearchParams((p) => {
      const next = new URLSearchParams(p);
      if (on) next.set("replay", "");
      else next.delete("replay");
      return next;
    });

  if (task === "not-found")
    return (
      <div className="empty-state">
        <p className="error-banner">Task not found.</p>
        <p className="muted">
          It may never have existed, or the backend was restarted without its database.
          <br />
          <Link to="/">← Back to all tasks</Link>
        </p>
      </div>
    );

  if (!task) return <p className="muted">Loading…</p>;

  const [title, ...restLines] = (task.task_description ?? "").trim().split("\n");
  const restOfDescription = restLines.join("\n").trim();
  // While a replay is still playing, the final outcome hasn't "happened" yet.
  const live = !replaying || replay.complete;
  const awaiting = !replaying && task.status === "awaiting_approval";
  const states = replaying ? replay.states : deriveStageStates(task);
  const stuck = live && (task.status === "failed" || task.status === "needs_human");
  const lastShown = shownEvents[shownEvents.length - 1];
  const asleep = replaying
    ? lastShown?.node === "human_approval" && lastShown.data.paused === true
    : awaiting && !!task.paused_at;
  const pausedAt = replaying
    ? typeof lastShown?.data.paused_at === "string" ? lastShown.data.paused_at : null
    : task.paused_at;
  const stuckId = task.status === "needs_human" ? stuckStage(task) : null;
  const stuckLabel = stuckId ? STAGES.find((s) => s.id === stuckId)?.label : null;
  const preferredTab =
    !live ? undefined
    : awaiting ? "diff"
    : stuckId === "reviewer" ? "review"
    : stuckId === "tester" ? "tests"
    : stuckId === "coding" ? "reply"
    : undefined;

  return (
    <div className="detail">
      <Link to="/" className="back-link">
        ← All tasks
      </Link>
      <div className="detail-head">
        <h2 className="detail-title">{title || <span className="muted">(no description)</span>}</h2>
        {!replaying && (
          <>
            <button className="btn replay-btn" onClick={() => setReplay(true)} title="Play this run back from its recorded events">
              ▶ Replay run
            </button>
            <span className={`conn ${connected ? "on" : "off"}`} title="Live event stream">
              {connected ? "● live" : "○ reconnecting"}
            </span>
          </>
        )}
      </div>
      <div className="meta-row">
        {live ? (
          <span className={`chip status-${task.status}`}>{task.status.replace(/_/g, " ")}</span>
        ) : (
          <span className="chip status-coding">replaying</span>
        )}
        <span className="mono" title={task.task_id}>{task.task_id.slice(0, 8)}</span>
        <span className="mono">{task.repo}</span>
        <span>base <span className="mono">{task.base_branch}</span></span>
        {task.cost_so_far > 0 && (
          <span className="mono" title="Total cost so far">
            ${task.cost_so_far.toFixed(4)}
            {(task.vm_cost ?? 0) > 0 && (
              <span className="muted"> · VM {task.vm_minutes?.toFixed(1) ?? "0"} min</span>
            )}
          </span>
        )}
        {task.pr_url && (
          <a href={task.pr_url} target="_blank" rel="noreferrer" className="btn pr-btn">
            View PR ↗
          </a>
        )}
      </div>
      {restOfDescription && <p className="desc-text">{restOfDescription}</p>}

      {replaying && <ReplayBar replay={replay} onExit={() => setReplay(false)} />}

      <StageTimeline
        states={states}
        retryCounts={replaying ? replay.retryCounts : task.retry_counts}
        stages={replaying ? replay.stages : undefined}
      />

      <SleepWakeCard
        events={shownEvents}
        pausedAt={pausedAt}
        asleep={asleep}
        vmMinutes={live ? task.vm_minutes : undefined}
      />

      {decideError && <p className="error-banner">{decideError}</p>}
      {stuck && (
        <section className="card attention">
          <div className="attention-head">
            <div>
              <h3>
                {task.status === "failed" ? "The run crashed" : "Needs your attention"}
                {stuckLabel && <span className="muted"> · stopped at {stuckLabel}</span>}
              </h3>
              <p className="muted">
                {task.status === "failed"
                  ? "Restarting re-drives the graph from its last checkpoint."
                  : "The pipeline gave up after its retry limit. Restarting resumes from the last checkpoint with the same retry counts, so fix the cause first (or approve manually)."}
              </p>
            </div>
            <button className="btn primary" onClick={restart} disabled={restarting}>
              {restarting ? "Restarting…" : "Restart from checkpoint"}
            </button>
          </div>
          {task.error_log.length > 0 && (
            <ul className="error-list">
              {task.error_log.map((e, i) => {
                const { node, msg } = splitError(e);
                return (
                  <li key={i}>
                    {node && <span className="mono node">{node}</span>}
                    <span>{msg}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
      {awaiting && <ApprovalPrompt onDecide={decide} busy={deciding} />}
      {live && !awaiting && !stuck && task.approval_status !== "pending" && (
        <p className="muted">
          Approval: <strong>{task.approval_status.replace("_", " ")}</strong>
          {task.paused_at && task.resumed_at && (
            <> · paused {new Date(task.paused_at).toLocaleTimeString()} → resumed {new Date(task.resumed_at).toLocaleTimeString()}</>
          )}
        </p>
      )}
      {live && !stuck && task.error_log.length > 0 && (
        <details className="card">
          <summary className="muted">{task.error_log.length} earlier error(s)</summary>
          <pre className="err">{task.error_log.join("\n")}</pre>
        </details>
      )}

      <div className="columns">
        <div className="event-log card" ref={logRef}>
          <h4>
            Events <span className="muted">({shownEvents.length})</span>
          </h4>
          {shownEvents.length === 0 && <p className="muted">Waiting for events…</p>}
          <ul>
            {shownEvents.map((ev, i) => (
              <EventRow key={ev.id ?? i} ev={ev} />
            ))}
          </ul>
        </div>

        <ArtifactsView
          artifacts={
            replaying && replay.artifacts
              ? replay.artifacts
              : {
                  plan: task.plan,
                  research_notes: task.research_notes,
                  code_diff: task.code_diff,
                  test_results: task.test_results,
                  review_comments: task.review_comments,
                }
          }
          codingReply={codingReply}
          preferred={preferredTab}
        />
      </div>
    </div>
  );
}
