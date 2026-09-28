import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, subscribeToEvents, ApiError } from "../api/client";
import type { PipelineEvent, TaskState } from "../api/types";
import { deriveStageStates } from "../components/stages";
import StageTimeline from "../components/StageTimeline";
import ApprovalPrompt from "../components/ApprovalPrompt";
import ArtifactsView from "../components/ArtifactsView";
import {
  BudgetMeter,
  BudgetPrompt,
  FollowupChat,
  Scoreboard,
  VisualProofView,
  WorkspaceStats,
} from "../components/Phase4Panels";

type NotFound = "not-found";

export default function TaskDetailPage() {
  const { taskId } = useParams<{ taskId: string }>();
  const [task, setTask] = useState<TaskState | NotFound | null>(null);
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [decideError, setDecideError] = useState<string | null>(null);
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
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

  const sendFollowup = async (message: string) => {
    if (!taskId) return false;
    setChatBusy(true);
    setChatError(null);
    try {
      await api.followup(taskId, message);
      await refetch();
      return true;
    } catch (e) {
      setChatError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setChatBusy(false);
    }
  };

  const raiseBudget = async (budget: number) => {
    if (!taskId) return;
    setDeciding(true);
    setDecideError(null);
    try {
      await api.raiseBudget(taskId, budget);
    } catch (e) {
      setDecideError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeciding(false);
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
  const awaitingBudget = task.status === "awaiting_budget";
  const states = deriveStageStates(task);

  return (
    <div className="detail">
      <div className="detail-head">
        <h2 className="mono">{task.task_id.slice(0, 8)}</h2>
        <span className={`chip status-${task.status}`}>{task.status.replace("_", " ")}</span>
        {task.harness && <span className="chip mono">{task.harness}</span>}
        <span className="muted mono">{task.repo}</span>
        <span className="muted">base: {task.base_branch}</span>
        {task.pr_url && (
          <a href={task.pr_url} target="_blank" rel="noreferrer" className="mono">
            {task.pr_url} ↗
          </a>
        )}
        <span className={`conn ${connected ? "on" : "off"}`} title="SSE stream">
          {connected ? "● live" : "○ reconnecting"}
        </span>
      </div>
      <p className="desc-text">{task.task_description}</p>

      <StageTimeline states={states} />

      {decideError && <p className="error-banner">{decideError}</p>}
      {awaiting && <ApprovalPrompt onDecide={decide} busy={deciding} />}
      {awaitingBudget && (
        <BudgetPrompt
          task={task}
          busy={deciding}
          onRaise={raiseBudget}
          onStop={() => decide("reject", "stopped at budget cap")}
        />
      )}

      <div className="panels">
        <BudgetMeter task={task} />
        <WorkspaceStats metrics={task.workspace_metrics ?? []} harness={task.harness} />
      </div>
      <Scoreboard rows={task.candidate_results ?? []} />
      <VisualProofView proof={task.visual_proof} />
      <FollowupChat task={task} onSend={sendFollowup} busy={chatBusy} error={chatError} />
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
