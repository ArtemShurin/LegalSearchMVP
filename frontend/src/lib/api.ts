import type {
  CollectionResponse,
  DocumentResponse,
  HistoryEntry,
  SourceItem,
  User,
} from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const TOKEN_KEY = "legalsearch-token";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = getToken();
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_URL}${path}`, { ...options, headers });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // тело не JSON — оставляем statusText
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// ---- auth ----

export async function register(email: string, password: string, companyName: string) {
  const data = await request<{ access_token: string }>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, company_name: companyName }),
  });
  setToken(data.access_token);
  return data;
}

export async function login(email: string, password: string) {
  const data = await request<{ access_token: string }>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  setToken(data.access_token);
  return data;
}

export function me() {
  return request<User>("/auth/me");
}

// ---- search (SSE-стрим) ----

export type SearchStreamHandlers = {
  onSources: (sources: SourceItem[]) => void;
  onToken: (text: string) => void;
  onDone: (queryId: string) => void;
};

/**
 * Бэкенд стримит ответ по SSE: событие "sources" (один раз), затем серия
 * "token" по мере генерации LLM, в конце "done" с query_id. Используем ручной
 * разбор потока через fetch (не EventSource — тому не передать POST/заголовки).
 */
export async function streamSearch(
  query: string,
  handlers: SearchStreamHandlers,
  signal?: AbortSignal,
  internalOnly?: boolean,
): Promise<void> {
  const token = getToken();
  const res = await fetch(`${API_URL}/search/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ query, filters: null, internal_only: !!internalOnly }),
    signal,
  });

  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // тело не JSON — оставляем statusText
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sepIndex: number;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      const rawEvent = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);

      let event = "message";
      let data = "";
      for (const line of rawEvent.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data = line.slice(5).trim();
      }
      if (!data) continue;
      const parsed = JSON.parse(data);

      if (event === "sources") handlers.onSources(parsed.sources);
      else if (event === "token") handlers.onToken(parsed.text);
      else if (event === "done") handlers.onDone(parsed.query_id);
    }
  }
}

// ---- history ----

export function getHistory() {
  return request<HistoryEntry[]>("/history/");
}

// ---- collections ----

export function getCollections() {
  return request<CollectionResponse[]>("/collections/");
}

export function createCollection(name: string) {
  return request<CollectionResponse>("/collections/", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export function renameCollection(id: string, name: string) {
  return request<CollectionResponse>(`/collections/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

export function deleteCollection(id: string) {
  return request<{ status: string }>(`/collections/${id}`, { method: "DELETE" });
}

export function addCollectionItem(
  collectionId: string,
  item: { document_id: string; title: string; fragment?: string | null; relevance?: number | null },
) {
  return request<{ status: string }>(`/collections/${collectionId}/items`, {
    method: "POST",
    body: JSON.stringify(item),
  });
}

export function deleteCollectionItem(collectionId: string, itemId: string) {
  return request<{ status: string }>(`/collections/${collectionId}/items/${itemId}`, {
    method: "DELETE",
  });
}

// ---- documents ----

export function getDocuments() {
  return request<DocumentResponse[]>("/documents/");
}

export async function uploadDocument(file: File): Promise<DocumentResponse> {
  const token = getToken();
  const form = new FormData();
  form.append("file", file);

  // Не используем общий request() — ему нельзя передать Content-Type вручную
  // для multipart (браузер сам должен сгенерировать boundary).
  const res = await fetch(`${API_URL}/documents/`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // тело не JSON
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export function deleteDocument(id: string) {
  return request<{ status: string }>(`/documents/${id}`, { method: "DELETE" });
}
