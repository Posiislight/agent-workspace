import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { TaskSummary } from "../api/types";

function StatusChip({ status }: { status: string }) {
  return <span className={`chip status-${status}`}>{status.replace(/_/g, " ")}</span>;
}

function ago(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

const NEEDS_YOU = new Set(["needs_human", "failed", "awaiting_approval"]);
const FINISHED = new Set(["done", "failed", "needs_human"]);

type Filter = "all" | "attention" | "running" | "done";
const FILTERS: { id: Filter; label: string; match: (status: string) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "attention", label: "Needs you", match: (s) => NEEDS_YOU.has(s) },
  { id: "running", label: "Running", match: (s) => !FINISHED.has(s) && s !== "awaiting_approval" },
  { id: "done", label: "Done", match: (s) => s === "done" },
];

function firstLine(s: string | null): string {
  return (s ?? "").trim().split("\n", 1)[0];
}

export default function TaskListPage() {
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<TaskSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  useEffect(() => {
    document.title = "Tasks · agent-workspace";
  }, []);

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

  const counts = useMemo(
    () =>
      Object.fromEntries(
        FILTERS.map((f) => [f.id, tasks?.filter((t) => f.match(t.status)).length ?? 0]),
      ) as Record<Filter, number>,
    [tasks],
  );

  const visible = useMemo(() => {
    if (!tasks) return [];
    const match = FILTERS.find((f) => f.id === filter)!.match;
    const q = query.trim().toLowerCase();
    return tasks.filter(
      (t) =>
        match(t.status) &&
        (!q ||
          t.task_id.toLowerCase().includes(q) ||
          t.repo.toLowerCase().includes(q) ||
          (t.task_description ?? "").toLowerCase().includes(q)),
    );
  }, [tasks, filter, query]);

  if (error && tasks === null) return <p className="error-banner">Failed to load tasks: {error}</p>;
  if (tasks === null) return <p className="muted">Loading…</p>;
  if (tasks.length === 0)
    return (
      <div className="empty-state">
        <h2>No tasks yet</h2>
        <p className="muted">Describe a change and the agents will code, test, review and open a PR.</p>
        <Link to="/new" className="btn primary">
          Create your first task
        </Link>
      </div>
    );

  return (
    <>
      <div className="list-head">
        <h2>Tasks</h2>
        <Link to="/new" className="btn primary">
          + New task
        </Link>
      </div>
      {error && <p className="warn-banner">Refresh failed ({error}); showing the last loaded list.</p>}
      <div className="list-toolbar">
        <div className="segmented" role="tablist" aria-label="Filter tasks">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              role="tab"
              aria-selected={filter === f.id}
              className={`seg${filter === f.id ? " active" : ""}${f.id === "attention" && counts.attention ? " hot" : ""}`}
              onClick={() => setFilter(f.id)}
            >
              {f.label} <span className="seg-count">{counts[f.id]}</span>
            </button>
          ))}
        </div>
        <input
          type="search"
          className="list-search"
          placeholder="Search id, repo or description…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <div className="table-wrap card">
        <table className="task-table">
          <thead>
            <tr>
              <th>Task</th>
              <th className="col-status">Status</th>
              <th className="hide-sm num col-cost">Cost</th>
              <th className="hide-sm col-updated">Updated</th>
              <th className="col-pr">PR</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((t) => (
              <tr
                key={t.task_id}
                tabIndex={0}
                className={NEEDS_YOU.has(t.status) ? "row-attention" : ""}
                onClick={(e) => {
                  if ((e.target as HTMLElement).closest("a")) return;
                  navigate(`/tasks/${t.task_id}`);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") navigate(`/tasks/${t.task_id}`);
                }}
              >
                <td className="task-cell">
                  <span className="desc" title={t.task_description ?? ""}>
                    {firstLine(t.task_description) || <span className="muted">(no description)</span>}
                  </span>
                  <span className="task-sub mono">
                    <Link to={`/tasks/${t.task_id}`} className="task-id">
                      {t.task_id.slice(0, 8)}
                    </Link>
                    <span title={t.repo}> · {t.repo}</span>
                  </span>
                </td>
                <td>
                  <StatusChip status={t.status} />
                </td>
                <td className="muted mono hide-sm num">
                  {typeof t.cost_so_far === "number" && t.cost_so_far > 0 ? `$${t.cost_so_far.toFixed(4)}` : "—"}
                </td>
                <td className="muted hide-sm nowrap" title={t.updated_at ? new Date(t.updated_at).toLocaleString() : ""}>
                  {ago(t.updated_at)}
                </td>
                <td>
                  {t.pr_url ? (
                    <a href={t.pr_url} target="_blank" rel="noreferrer" className="nowrap">
                      PR ↗
                    </a>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {visible.length === 0 && <p className="muted table-empty">No tasks match.</p>}
      </div>
    </>
  );
}
