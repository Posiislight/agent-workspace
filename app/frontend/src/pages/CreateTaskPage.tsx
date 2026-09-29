import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { TaskCreateResponse } from "../api/types";
import SearchableSelect from "../components/SearchableSelect";
import type { Option } from "../components/SearchableSelect";

interface Repo {
  full_name: string;
  private: boolean;
  default_branch: string;
}

interface TemplateInfo {
  id: string;
  name: string;
  description: string;
  tags: string[];
}

export default function CreateTaskPage() {
  const navigate = useNavigate();
  const [description, setDescription] = useState("");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [reposLoading, setReposLoading] = useState(true);
  const [reposError, setReposError] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(true);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [templateId, setTemplateId] = useState("codex");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "New task · agent-workspace";
    api.githubRepos().then(
      (rs) => {
        setRepos(rs);
        setReposLoading(false);
      },
      (e) => {
        setReposError(e instanceof Error ? e.message : String(e));
        setReposLoading(false);
      },
    );
    api.templates().then(
      (ts) => {
        setTemplates(ts);
        setTemplatesLoading(false);
      },
      (e) => {
        setTemplatesError(e instanceof Error ? e.message : String(e));
        setTemplatesLoading(false);
      },
    );
  }, []);

  const repoOptions: Option[] = useMemo(
    () => repos.map((r) => ({ value: r.full_name, label: r.full_name, hint: r.private ? "private" : undefined })),
    [repos],
  );

  const templateOptions: Option[] = useMemo(
    () => templates.map((t) => ({ value: t.id, label: t.name, hint: t.description })),
    [templates],
  );

  const pickRepo = (value: string) => {
    setRepo(value);
    const r = repos.find((x) => x.full_name === value);
    if (r?.default_branch) setBaseBranch(r.default_branch);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!description.trim() || !repo.trim()) {
      setError("Description and repo are required.");
      return;
    }
    setSubmitting(true);
    try {
      const res: TaskCreateResponse = await api.createTask({
        task_description: description.trim(),
        repo: repo.trim(),
        base_branch: baseBranch.trim() || "main",
        test_command: null,
        template_id: templateId,
      });
      navigate(`/tasks/${res.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <form className="create-form card" onSubmit={submit}>
      <div>
        <h2>New task</h2>
        <p className="muted form-sub">
          The coding agent implements it, the tester and reviewer check it, and you approve before a PR opens.
        </p>
      </div>

      <label>
        Description
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What should the agents build or fix? Be specific about files, behaviour and acceptance criteria."
          rows={6}
          autoFocus
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.form?.requestSubmit();
          }}
          required
        />
      </label>

      <div className="row">
        <label>
          Repository
          {reposError ? (
            <>
              <input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="acme/widgets"
                pattern="[\w.-]+/[\w.-]+"
                required
              />
              <span className="muted error-banner">Couldn't load repos: {reposError}</span>
            </>
          ) : (
            <SearchableSelect
              options={repoOptions}
              value={repo}
              onChange={pickRepo}
              placeholder={reposLoading ? "Loading repositories…" : "Select a repository…"}
              loading={reposLoading}
            />
          )}
        </label>
        <label>
          Base branch
          <input value={baseBranch} onChange={(e) => setBaseBranch(e.target.value)} placeholder="main" />
        </label>
      </div>

      <label>
        <span>
          Agent template <span className="muted field-hint">· Maritime harness powering every stage</span>
        </span>
        {templatesError && (
          <span className="muted error-banner">Couldn't load templates: {templatesError}</span>
        )}
        <SearchableSelect
          options={templateOptions}
          value={templateId}
          onChange={setTemplateId}
          placeholder={templatesLoading ? "Loading templates…" : "Select a template…"}
          loading={templatesLoading}
        />
      </label>

      {error && <p className="error-banner">{error}</p>}

      <div className="form-actions">
        <span className="muted field-hint">Ctrl + Enter to submit</span>
        <button type="submit" className="btn primary" disabled={submitting}>
          {submitting ? "Starting pipeline…" : "Start pipeline →"}
        </button>
      </div>
    </form>
  );
}
