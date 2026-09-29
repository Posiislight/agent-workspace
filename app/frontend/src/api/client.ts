import type {
  Artifacts,
  PipelineEvent,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskState,
  TaskSummary,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  createTask: (body: TaskCreateRequest) =>
    request<TaskCreateResponse>("/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getTask: (id: string) => request<TaskState>(`/tasks/${id}`),

  listTasks: () =>
    request<{ tasks: TaskSummary[] }>("/tasks").then((r) => r.tasks ?? []),

  approve: (id: string, feedback: string) =>
    request<unknown>(`/tasks/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ feedback }),
    }),

  reject: (id: string, feedback: string) =>
    request<unknown>(`/tasks/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ feedback }),
    }),

  restart: (id: string) =>
    request<unknown>(`/tasks/${id}/restart`, { method: "POST" }),

  artifacts: (id: string) => request<Artifacts>(`/tasks/${id}/artifacts`),

  eventsUrl: (id: string) => `/tasks/${id}/events`,

  eventHistory: (id: string) =>
    request<{ events: PipelineEvent[] }>(`/tasks/${id}/events/history`).then((r) => r.events ?? []),

  githubRepos: () =>
    request<{ repos: { full_name: string; private: boolean; default_branch: string }[] }>(
      "/github/repos",
    ).then((r) => r.repos ?? []),

  templates: () =>
    request<{ templates: { id: string; name: string; description: string; tags: string[] }[] }>(
      "/templates",
    ).then((r) => r.templates ?? []),
};

// SSE event name is the event `type` field, so register a listener per type.
const EVENT_TYPES = [
  "node_started",
  "node_completed",
  "tool_call",
  "cost_update",
  "sleep_wake",
] as const;

export function subscribeToEvents(
  taskId: string,
  onEvent: (ev: PipelineEvent) => void,
  onStateChange?: (connected: boolean) => void,
): () => void {
  let es: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const connect = () => {
    if (closed) return;
    es = new EventSource(api.eventsUrl(taskId));
    es.onopen = () => onStateChange?.(true);
    es.onerror = () => {
      onStateChange?.(false);
      es?.close();
      es = null;
      // Reconnect with backoff; server replays from the beginning of the
      // stream, so consumers must de-duplicate by event id.
      retry = setTimeout(connect, 2000);
    };
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, (msg) => {
        try {
          const m = msg as MessageEvent;
          const ev = JSON.parse(m.data) as PipelineEvent;
          if (m.lastEventId) ev.id = m.lastEventId;
          onEvent(ev);
        } catch {
          /* malformed event, ignore */
        }
      });
    }
  };

  connect();
  return () => {
    closed = true;
    if (retry) clearTimeout(retry);
    es?.close();
  };
}
