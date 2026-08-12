import type { ChatAttachment } from "../types/chat";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const MIN_ATTACHMENT_TIMEOUT_MS = 1_000;
const MAX_UPLOAD_TIMEOUT_MS = 30 * 60 * 1_000;
const MAX_DELETE_TIMEOUT_MS = 5 * 60 * 1_000;

function envTimeoutMs(name: string, fallback: number, max: number): number {
  const raw = import.meta.env[name];
  const parsed = typeof raw === "string" && raw.trim() ? Number(raw) : fallback;
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(MIN_ATTACHMENT_TIMEOUT_MS, Math.trunc(parsed)));
}

export const ATTACHMENT_UPLOAD_TIMEOUT_MS = envTimeoutMs(
  "VITE_ATTACHMENT_UPLOAD_TIMEOUT_MS",
  10 * 60 * 1000,
  MAX_UPLOAD_TIMEOUT_MS,
);
export const ATTACHMENT_DELETE_TIMEOUT_MS = envTimeoutMs(
  "VITE_ATTACHMENT_DELETE_TIMEOUT_MS",
  30 * 1000,
  MAX_DELETE_TIMEOUT_MS,
);

export interface ChatAttachmentUpload {
  promise: Promise<ChatAttachment>;
  abort: () => void;
}

function apiUrl(path: string): string {
  if (!API_BASE_URL || !path.startsWith("/")) return path;
  return `${API_BASE_URL}${path}`;
}

function attachmentFieldError(field: string, expected: string): Error {
  return new Error(
    `Attachment upload response field ${field} must be ${expected}`,
  );
}

function requireString(record: Record<string, unknown>, field: string): string {
  const value = record[field];
  if (typeof value !== "string") throw attachmentFieldError(field, "a string");
  return value;
}

function requireNumber(record: Record<string, unknown>, field: string): number {
  const value = record[field];
  if (typeof value !== "number") throw attachmentFieldError(field, "a number");
  return value;
}

async function fetchAttachmentMaxFileBytes(
  signal: AbortSignal,
): Promise<number> {
  const response = await fetch(apiUrl("/api/chat/attachments/limits"), {
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    throw new Error(
      `Unable to load attachment limit: ${response.statusText || response.status}`,
    );
  }
  const value = (await response.json()) as { max_file_bytes?: unknown };
  const maxFileBytes = value.max_file_bytes;
  if (
    typeof maxFileBytes !== "number" ||
    !Number.isSafeInteger(maxFileBytes) ||
    maxFileBytes <= 0
  ) {
    throw new Error(
      "Attachment limit response field max_file_bytes must be a positive integer",
    );
  }
  return maxFileBytes;
}

function parseChatAttachment(value: unknown): ChatAttachment {
  if (!value || typeof value !== "object") {
    throw new Error("Attachment upload returned invalid payload");
  }
  const record = value as Record<string, unknown>;
  return {
    ...record,
    id: requireString(record, "id"),
    project_id: requireString(record, "project_id"),
    filename: requireString(record, "filename"),
    mime_type: requireString(record, "mime_type"),
    size_bytes: requireNumber(record, "size_bytes"),
    content_url: requireString(record, "content_url"),
  } as ChatAttachment;
}

function errorFromResponse(xhr: XMLHttpRequest): string {
  try {
    const parsed = JSON.parse(xhr.responseText) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
  } catch {
    // Fall through to status text.
  }
  return xhr.statusText || "Attachment upload failed";
}

export function normalizeAttachmentUrl(
  attachment: ChatAttachment,
): ChatAttachment {
  let contentUrl = attachment.content_url;
  if (!contentUrl.startsWith("/") && typeof window !== "undefined") {
    try {
      const url = new URL(contentUrl, window.location.origin);
      if (url.origin === window.location.origin) {
        contentUrl = `${url.pathname}${url.search}${url.hash}`;
      }
    } catch {
      // Renderer safety checks handle malformed or external URLs.
    }
  }
  return {
    ...attachment,
    content_url: contentUrl,
  };
}

export function uploadChatAttachment(
  file: File,
  options: {
    draftId?: string;
    projectId?: string | null;
    onProgress?: (progress: number | null) => void;
  } = {},
): ChatAttachmentUpload {
  const limitRequest = new AbortController();
  let xhr: XMLHttpRequest | null = null;
  let canceled = false;
  const promise = (async (): Promise<ChatAttachment> => {
    let maxFileBytes: number;
    try {
      maxFileBytes = await fetchAttachmentMaxFileBytes(limitRequest.signal);
    } catch (error) {
      if (canceled) throw new Error("Attachment upload canceled");
      throw error;
    }
    if (file.size > maxFileBytes) {
      throw new Error(
        `Attachment exceeds ${formatAttachmentSize(maxFileBytes)} limit`,
      );
    }
    if (canceled) throw new Error("Attachment upload canceled");

    const uploadXhr = new XMLHttpRequest();
    xhr = uploadXhr;
    return new Promise<ChatAttachment>((resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      if (options.draftId) form.append("draft_id", options.draftId);
      if (options.projectId) form.append("project_id", options.projectId);

      uploadXhr.open("POST", apiUrl("/api/chat/attachments"));
      uploadXhr.withCredentials = true;
      uploadXhr.timeout = ATTACHMENT_UPLOAD_TIMEOUT_MS;
      uploadXhr.upload.onprogress = (event) => {
        options.onProgress?.(
          event.lengthComputable ? event.loaded / event.total : null,
        );
      };
      uploadXhr.onload = () => {
        if (uploadXhr.status >= 200 && uploadXhr.status < 300) {
          try {
            resolve(
              normalizeAttachmentUrl(
                parseChatAttachment(JSON.parse(uploadXhr.responseText)),
              ),
            );
          } catch (error) {
            reject(
              error instanceof SyntaxError
                ? new Error("Attachment upload returned invalid JSON")
                : error,
            );
          }
          return;
        }
        reject(new Error(errorFromResponse(uploadXhr)));
      };
      uploadXhr.onerror = () => {
        options.onProgress?.(null);
        reject(new Error("Attachment upload failed"));
      };
      uploadXhr.onabort = () => {
        options.onProgress?.(null);
        reject(new Error("Attachment upload canceled"));
      };
      uploadXhr.ontimeout = () => {
        options.onProgress?.(null);
        reject(new Error("Attachment upload timed out"));
      };
      uploadXhr.send(form);
    });
  })();
  return {
    promise,
    abort: () => {
      canceled = true;
      limitRequest.abort();
      xhr?.abort();
    },
  };
}

export async function deleteChatAttachment(
  attachmentId: string,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(
    () => controller.abort(),
    ATTACHMENT_DELETE_TIMEOUT_MS,
  );
  try {
    const response = await fetch(
      apiUrl(`/api/chat/attachments/${encodeURIComponent(attachmentId)}`),
      {
        method: "DELETE",
        credentials: "include",
        signal: controller.signal,
      },
    );
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(
        body ||
          response.statusText ||
          `Attachment delete failed (${response.status})`,
      );
    }
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Attachment delete timed out");
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes <= 0) return "0 B";
  if (bytes >= 1024 * 1024 * 1024)
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${bytes} B`;
}
