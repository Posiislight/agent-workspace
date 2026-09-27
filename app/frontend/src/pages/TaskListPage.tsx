import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { TaskSummary } from "../api/types";

function StatusChip({ status }: { status: string }) {
  return <span className={`chip status-${status}`}>{status.replace("_", " ")}</span>;
}

export default function TaskListPage() {
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setTasks(await api.listTasks());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [refresh]);

  if (error) return <p className="error-banner">Failed to load tasks: {error}</p>;
  if (tasks === null) return <p className="muted">Loading…</p>;
  if (tasks.length === 0)
    return (
      <p className="muted empty-state">
        No tasks yet. <Link to="/new">Create one</Link> to kick off the pipeline.
      </p>
    );

  return (
    <table className="task-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>Repo</th>
          <th>Description</th>
          <th>Stage</th>
          <th>Cost</th>
          <th>Updated</th>
          <th>PR</th>
        </tr>
      </thead>
      <tbody>
        {tasks.map((t) => (
          <tr key={t.task_id}>
            <td>
              <Link to={`/tasks/${t.task_id}`} className="mono">
                {t.task_id.slice(0, 8)}
              </Link>
            </td>
            <td className="mono">{t.repo}</td>
            <td className="desc" title={t.task_description ?? ""}>
              {t.task_description || "—"}
            </td>
            <td>
              <StatusChip status={t.status} />
            </td>
            <td className="muted mono">
              {typeof t.cost_so_far === "number" ? `$${t.cost_so_far.toFixed(4)}` : "—"}
            </td>
            <td className="muted">
              {t.updated_at ? new Date(t.updated_at).toLocaleTimeString() : "—"}
            </td>
            <td>
              {t.pr_url ? (
                <a href={t.pr_url} target="_blank" rel="noreferrer">
                  PR ↗
                </a>
              ) : (
                "—"
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
