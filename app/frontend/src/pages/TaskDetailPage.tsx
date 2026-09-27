import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, subscribeToEvents, ApiError } from "../api/client";
import type { PipelineEvent, TaskState } from "../api/types";
import { deriveStageStates } from "../components/stages";
import StageTimeline from "../components/StageTimeline";
import ApprovalPrompt from "../components/ApprovalPrompt";
import ArtifactsView from "../components/ArtifactsView";

type NotFound = "not-found";

export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
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

    const onEvent = (ev: PipelineEvent) => {
      setEvents((prev) => {
        const next = [...prev, ev];
        return next.length > 500 ? next.slice(-500) : next;
      });
      refetch();
    };
    const unsub = subscribeToEvents(taskId, onEvent, setConnected);
    return unsub;
  }, [taskId, refetch]);

  // Auto-scroll event log
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

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

  const awaiting = task.status === "awaiting_approval";
  const states = deriveStageStates(task);

  return (
    <div className="detail">
      <div className="detail-head">
        <h2 className="mono">{task.task_id.slice(0, 8)}</h2>
        <span className={`chip status-${task.status}`}>{task.status.replace("_", " ")}</span>
        <span className="muted mono">{task.repo}</span>
        <span className="muted">base: {task.base_branch}</span>
        {task.pr_url && (
          <a href={task.pr_url} target="_blank" rel="noreferrer" className="mono">
            {task.pr_url} ↗
          </a>
        )}
        {task.cost_so_far > 0 && (
          <span className="chip cost" title="LLM cost so far">
            ${task.cost_so_far.toFixed(4)}
          </span>
        )}
        {(task.vm_cost ?? 0) > 0 && (
          <span className="chip cost" title="VM awake-time cost">
            +${task.vm_cost!.toFixed(4)} VM ({task.vm_minutes?.toFixed(1) ?? "0"} min)
          </span>
        )}
        <span className={`conn ${connected ? "on" : "off"}`} title="SSE stream">
          {connected ? "● live" : "○ reconnecting"}
        </span>
      </div>
      <p className="desc-text">{task.task_description}</p>

      <StageTimeline states={states} />

      {decideError && <p className="error-banner">{decideError}</p>}
      {(task.status === "failed" || task.status === "needs_human") && (
        <div className="card">
          <p className="muted">
            {task.status === "failed"
              ? "The run crashed. Restarting re-drives the graph from its last checkpoint."
              : "Retry bounds were exceeded. Restarting resumes from the last checkpoint."}
          </p>
          <button onClick={restart} disabled={restarting}>
            {restarting ? "Restarting…" : "Restart from checkpoint"}
          </button>
        </div>
      )}
      {awaiting && <ApprovalPrompt onDecide={decide} busy={deciding} />}
      {!awaiting && task.approval_status !== "pending" && (
        <p className="muted">
          Approval: <strong>{task.approval_status}</strong>
          {task.paused_at && task.resumed_at && (
            <> · paused {new Date(task.paused_at).toLocaleTimeString()} → resumed {new Date(task.resumed_at).toLocaleTimeString()}</>
          )}
        </p>
      )}

      {task.error_log.length > 0 && (
        <section className="card artifact">
          <h4>Errors</h4>
          <pre className="err">{task.error_log.join("\n")}</pre>
        </section>
      )}

      <div className="columns">
        <div className="event-log card" ref={logRef}>
          <h4>Event stream</h4>
          {events.length === 0 && <p className="muted">Waiting for events…</p>}
          <ul>
            {events.map((ev, i) => (
              <li key={i}>
                <span className="muted mono ts">{new Date(ev.ts).toLocaleTimeString()}</span>
                <span className="mono node">{ev.node}</span>
                <span className={`chip ev-${ev.type}`}>{ev.type}</span>
                {Object.keys(ev.data).length > 0 && (
                  <span className="ev-data">{JSON.stringify(ev.data)}</span>
                )}
              </li>
            ))}
          </ul>
        </div>

        <ArtifactsView
          artifacts={{
            plan: task.plan,
            research_notes: task.research_notes,
            code_diff: task.code_diff,
            test_results: task.test_results,
            review_comments: task.review_comments,
          }}
        />
      </div>
    </div>
  );
}
