import { parseSse } from "./sse";

export interface Me {
  username: string;
  display_name: string;
  is_admin: boolean;
  folders: string[];
  upload_folders: string[];
  upload_types: string[];
  max_upload_mb: number;
  csrf_token: string;
}

export interface Source {
  id: string;
  filename: string;
  title: string;
  folder: string;
  sections: string[];
  refs: number[];
  snippet: string;
  n_versions: number;
}

export interface Candidate {
  id: string;
  filename: string;
  title: string;
  folder: string;
}

export interface Answer {
  qa_id: number;
  status: "answered" | "not_found" | "needs_choice";
  text: string;
  mode: string;
  ungrounded: boolean;
  note: string;
  resolved: { id: string; filename: string; title: string; folder: string; n_versions: number } | null;
  sources: Source[];
  candidates: Candidate[];
}

export interface VersionInfo {
  id: string;
  filename: string;
  modified: string;
  latest: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

let csrf = "";
let onUnauthorized: () => void = () => {};

export function setCsrf(token: string): void {
  csrf = token;
}

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn;
}

async function failure(r: Response): Promise<ApiError> {
  let message = `Request failed (${r.status}).`;
  try {
    const body = await r.json();
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail)) message = "That request was not valid.";
  } catch {
    /* not JSON */
  }
  return new ApiError(r.status, message);
}

async function request<T>(path: string, init: RequestInit = {}, sessionRequired = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.method && init.method !== "GET") headers.set("X-CSRF-Token", csrf);
  const r = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!r.ok) {
    if (r.status === 401 && sessionRequired) onUnauthorized();
    throw await failure(r);
  }
  return (await r.json()) as T;
}

export async function getMe(): Promise<Me | null> {
  const r = await fetch("/api/me", { credentials: "same-origin" });
  if (r.status === 401) return null;
  if (!r.ok) throw await failure(r);
  const me = (await r.json()) as Me;
  setCsrf(me.csrf_token);
  return me;
}

export async function login(username: string, password: string): Promise<Me> {
  const me = await request<Me>(
    "/api/auth/login",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    },
    false,
  );
  setCsrf(me.csrf_token);
  return me;
}

export async function logout(): Promise<void> {
  await request("/api/auth/logout", { method: "POST" });
  setCsrf("");
}

export function versions(id: string): Promise<VersionInfo[]> {
  return request<VersionInfo[]>(`/api/documents/${encodeURIComponent(id)}/versions`);
}

export function sendFeedback(qaId: number, value: 1 | -1 | null): Promise<unknown> {
  return request(`/api/qa/${qaId}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
}

export interface UploadResult {
  path: string;
  filename: string;
  folder: string;
  indexed: boolean;
  id: string | null;
}

export function upload(folder: string, file: File): Promise<UploadResult> {
  const form = new FormData();
  form.set("folder", folder);
  form.set("file", file);
  return request<UploadResult>("/api/upload", { method: "POST", body: form });
}

export function downloadUrl(id: string): string {
  return `/api/documents/${encodeURIComponent(id)}/download`;
}

export interface ChatHandlers {
  onStatus: (state: string) => void;
  onToken: (text: string) => void;
  onAnswer: (answer: Answer) => void;
  onError: (message: string) => void;
}

/** Ask a question and stream the reply. Resolves when the stream ends; aborting is quiet. */
export async function streamChat(
  question: string,
  pinned: string | null,
  h: ChatHandlers,
  signal: AbortSignal,
): Promise<void> {
  let r: Response;
  try {
    r = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ question, pinned }),
      signal,
    });
  } catch {
    if (!signal.aborted) h.onError("Could not reach the server.");
    return;
  }
  if (r.status === 401) {
    onUnauthorized();
    return;
  }
  if (!r.ok || !r.body) {
    h.onError((await failure(r)).message);
    return;
  }
  const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let finished = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const parsed = parseSse(buffer + value);
      buffer = parsed.rest;
      for (const { event, data } of parsed.events) {
        const d = data as Record<string, unknown>;
        if (event === "status") h.onStatus(String(d.state));
        else if (event === "token") h.onToken(String(d.text));
        else if (event === "answer") {
          finished = true;
          h.onAnswer(data as Answer);
        } else if (event === "error") {
          finished = true;
          h.onError(String(d.message));
        }
      }
    }
  } catch {
    if (signal.aborted) return;
  }
  if (!finished && !signal.aborted) h.onError("The connection was interrupted. Try again.");
}
