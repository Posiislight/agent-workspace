import { useState } from "react";
import type {
  CandidateRow,
  FollowupEntry,
  TaskState,
  VisualProof,
  WorkspaceMetric,
} from "../api/types";

const usd = (n: number | undefined | null, digits = 4) => `$${(n ?? 0).toFixed(digits)}`;

/** Live LLM + VM-minute meter against the task budget. */
export function BudgetMeter({ task }: { task: TaskState }) {
  const llm = task.llm_cost ?? 0;
  const vm = task.vm_cost ?? 0;
  const total = task.cost_so_far ?? llm + vm;
  const budget = task.budget_usd ?? 0;
  const pct = budget > 0 ? Math.min(100, (total / budget) * 100) : 0;
  const llmPct = budget > 0 ? Math.min(100, (llm / budget) * 100) : 0;
  return (
    <section className="card meter">
      <div className="meter-head">
        <h4>Cost</h4>
        <span className="mono">
          {usd(total)}
          {budget > 0 && <span className="muted"> / {usd(budget, 2)} budget</span>}
        </span>
      </div>
      {budget > 0 && (
        <div className={`meter-bar ${pct >= 100 ? "over" : pct >= 80 ? "warn" : ""}`}>
          <span className="meter-llm" style={{ width: `${llmPct}%` }} />
          <span className="meter-vm" style={{ width: `${Math.max(0, pct - llmPct)}%` }} />
        </div>
      )}
      <p className="muted meter-legend">
        <span className="dot llm" /> LLM {usd(llm)} · <span className="dot vm" /> VM {usd(vm)} (
        {Math.round(task.vm_seconds ?? 0)}s awake) — sleeping VMs cost nothing
      </p>
    </section>
  );
}

export function BudgetPrompt({
  task,
  onRaise,
  onStop,
  busy,
}: {
  task: TaskState;
  onRaise: (budget: number) => void;
  onStop: () => void;
  busy: boolean;
}) {
  const suggested = Math.max((task.budget_usd ?? 0) * 2, (task.cost_so_far ?? 0) + 0.25);
  const [value, setValue] = useState(suggested.toFixed(2));
  return (
    <section className="approval card">
      <h3>Budget reached</h3>
      <p className="muted">
        The agent spent {usd(task.cost_so_far)} of its {usd(task.budget_usd, 2)} budget and went to
        sleep before spending more. Raise the cap to continue, or stop here.
      </p>
      <div className="row">
        <label>
          New budget (USD)
          <input
            type="number"
            min="0.01"
            step="0.05"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
      </div>
      <div className="row buttons">
        <button
          className="btn success"
          disabled={busy || !(Number(value) > 0)}
          onClick={() => onRaise(Number(value))}
        >
          Raise &amp; continue
        </button>
        <button className="btn danger" disabled={busy} onClick={onStop}>
          Stop
        </button>
      </div>
    </section>
  );
}

/** Best-of-N scoreboard. */
export function Scoreboard({ rows }: { rows: CandidateRow[] }) {
  if (!rows.length) return null;
  return (
    <section className="card">
      <h4>Best-of-{rows.length} scoreboard</h4>
      <div className="table-scroll">
      <table className="task-table scoreboard">
        <thead>
          <tr>
            <th>#</th>
            <th>Harness</th>
            <th>Status</th>
            <th>Tests</th>
            <th>Diff</th>
            <th>Time</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.index} className={r.winner ? "winner" : ""}>
              <td className="mono">{r.index}</td>
              <td className="mono">
                {r.harness}
                {r.slot > 0 && <span className="muted"> /{r.slot}</span>}
              </td>
              <td>
                <span className={`chip cand-${r.status}`}>{r.status.replace("_", " ")}</span>
              </td>
              <td>{r.tests_passed == null ? "—" : r.tests_passed ? "pass" : "fail"}</td>
              <td className="mono">
                <span className="add">+{r.insertions}</span>/<span className="del">-{r.deletions}</span>{" "}
                <span className="muted">({r.files} files)</span>
              </td>
              <td className="mono">{r.seconds ? `${r.seconds}s` : "—"}</td>
              <td className="mono">
                {r.score ?? "—"}
                {r.winner && <span className="chip winner-chip">winner</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      <p className="muted small">
        Candidates run in their own persistent VMs, two awake at a time; the rest wait asleep.
      </p>
    </section>
  );
}

export function VisualProofView({ proof }: { proof: VisualProof | null | undefined }) {
  if (!proof) return null;
  const { before, after } = proof.images ?? {};
  return (
    <section className="card">
      <h4>Visual proof</h4>
      {proof.error && <p className="failing">Proof failed: {proof.error}</p>}
      {(before || after) && (
        <div className="proof-grid">
          {(["before", "after"] as const).map((k) =>
            proof.images?.[k] ? (
              <figure key={k}>
                <a href={proof.images[k]} target="_blank" rel="noreferrer">
                  <img src={proof.images[k]} alt={`${k} screenshot`} />
                </a>
                <figcaption className="muted">{k}</figcaption>
              </figure>
            ) : null,
          )}
        </div>
      )}
      {proof.preview && (
        <p className="muted small mono">
          {proof.preview.command} → :{proof.preview.port}
          {proof.preview.path}
        </p>
      )}
    </section>
  );
}

export function WorkspaceStats({ metrics, harness }: { metrics: WorkspaceMetric[]; harness?: string }) {
  if (!metrics.length) return null;
  return (
    <section className="card">
      <h4>Repo agent {harness && <span className="muted mono">({harness})</span>}</h4>
      <ul className="ws-list">
        {metrics.slice(-6).map((m, i) => (
          <li key={i}>
            <span className={`chip ${m.cold ? "cold" : "warm"}`}>{m.cold ? "cold start" : "warm"}</span>
            <span className="mono">{m.ready_seconds.toFixed(1)}s ready</span>
            <span className="muted">deps {m.deps}</span>
            {m.queued_seconds > 0.5 && (
              <span className="muted">queued {m.queued_seconds.toFixed(1)}s for a VM</span>
            )}
            <span className="muted mono small">{m.agent}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

const SOURCE_LABEL: Record<string, string> = { chat: "you", pr_comment: "PR comment", ci: "CI" };

/** Keep chatting after the PR: same agent, same VM, same branch. */
export function FollowupChat({
  task,
  onSend,
  busy,
  error,
}: {
  task: TaskState;
  onSend: (msg: string) => Promise<boolean>;
  busy: boolean;
  error: string | null;
}) {
  const [msg, setMsg] = useState("");
  const history: FollowupEntry[] = task.followups ?? [];
  const canSend = ["awaiting_approval", "done", "needs_human"].includes(task.status);
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!msg.trim()) return;
    if (await onSend(msg.trim())) setMsg("");
  };
  if (!task.pr_url && history.length === 0 && task.status !== "needs_human") return null;
  return (
    <section className="card chat">
      <h4>Keep chatting</h4>
      <p className="muted small">
        Messages wake the same agent on the same VM and branch; it edits, tests and pushes to
        the PR, then goes back to sleep. Comment <code>/aw …</code> on the PR to do the same from
        GitHub; failed CI checks trigger a fix automatically.
      </p>
      {history.length > 0 && (
        <ul className="chat-log">
          {history.map((f, i) => (
            <li key={i}>
              <span className={`chip src-${f.source}`}>{SOURCE_LABEL[f.source] ?? f.source}</span>
              <span className="chat-text">{f.instruction.split("\n")[0]}</span>
              <span className="muted small">{new Date(f.at).toLocaleTimeString()}</span>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="error-banner">{error}</p>}
      <form className="chat-form" onSubmit={send}>
        <input
          value={msg}
          onChange={(e) => setMsg(e.target.value)}
          placeholder={canSend ? "e.g. make it async, add a docstring…" : "Agent is working — wait for it to pause"}
          disabled={!canSend || busy}
        />
        <button className="btn primary" disabled={!canSend || busy || !msg.trim()}>
          {busy ? "Waking…" : "Send"}
        </button>
      </form>
    </section>
  );
}
