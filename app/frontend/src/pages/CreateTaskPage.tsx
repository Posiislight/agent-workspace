import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { TaskCreateResponse } from "../api/types";

interface OverrideRow {
  key: string;
  value: string;
}

export default function CreateTaskPage() {
  const navigate = useNavigate();
  const [description, setDescription] = useState("");
  const [repo, setRepo] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [testCommand, setTestCommand] = useState("");
  const [overrides, setOverrides] = useState<OverrideRow[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const setOverride = (i: number, patch: Partial<OverrideRow>) =>
    setOverrides((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!description.trim() || !repo.trim()) {
      setError("Description and repo are required.");
      return;
    }
    setSubmitting(true);
    try {
      const model_overrides = Object.fromEntries(
        overrides
          .filter((r) => r.key.trim())
          .map((r) => [r.key.trim(), r.value]),
      );
      const res: TaskCreateResponse = await api.createTask({
        task_description: description.trim(),
        repo: repo.trim(),
        base_branch: baseBranch.trim() || "main",
        test_command: testCommand.trim() || null,
        model_overrides,
      });
      navigate(`/tasks/${res.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <form className="create-form" onSubmit={submit}>
      <h2>Create task</h2>

      <label>
        Description
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What should the agents build or fix?"
          rows={5}
          required
        />
      </label>

      <div className="row">
        <label>
          Repo <span className="muted">(org/name)</span>
          <input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="acme/widgets"
            pattern="[\w.-]+/[\w.-]+"
            required
          />
        </label>
        <label>
          Base branch
          <input value={baseBranch} onChange={(e) => setBaseBranch(e.target.value)} placeholder="main" />
        </label>
      </div>

      <label>
        Test command <span className="muted">(optional)</span>
        <input
          value={testCommand}
          onChange={(e) => setTestCommand(e.target.value)}
          placeholder="pytest -x -q"
        />
      </label>

      <fieldset>
        <legend>
          Model overrides <span className="muted">(optional per-stage models)</span>
        </legend>
        {overrides.map((r, i) => (
          <div className="row override-row" key={i}>
            <input
              value={r.key}
              onChange={(e) => setOverride(i, { key: e.target.value })}
              placeholder="planner"
            />
            <input
              value={r.value}
              onChange={(e) => setOverride(i, { value: e.target.value })}
              placeholder="openai/gpt-4o"
            />
            <button
              type="button"
              className="btn ghost"
              onClick={() => setOverrides((rows) => rows.filter((_, idx) => idx !== i))}
            >
              ✕
            </button>
          </div>
        ))}
        <button type="button" className="btn ghost" onClick={() => setOverrides((r) => [...r, { key: "", value: "" }])}>
          + Add override
        </button>
      </fieldset>

      {error && <p className="error-banner">{error}</p>}

      <button type="submit" className="btn primary" disabled={submitting}>
        {submitting ? "Starting pipeline…" : "Start pipeline"}
      </button>
    </form>
  );
}
