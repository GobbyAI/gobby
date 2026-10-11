import {
  extractTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";

export type TaskActionEndpoint = "release-claim" | "close" | "reopen";

/**
 * PATCH-safe task fields only. Mirrors the backend TaskUpdateRequest subset
 * that the PATCH route accepts. Stage and terminal actions route to dedicated
 * endpoints instead.
 */
export type PatchTaskFields = {
  title?: string;
  description?: string;
  priority?: number;
  task_type?: string;
  category?: string;
  labels?: string[];
  validation_criteria?: string;
};

export function extractResponseErrorMessage(
  body: string,
  statusText: string,
  fallback: string,
  status: number,
): string {
  const fallbackWithStatus = `${fallback} (${status})`;
  const trimmed = body.trim();
  if (trimmed) {
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      for (const key of ["detail", "error", "message"]) {
        const value = parsed[key];
        if (typeof value === "string" && value.trim()) return value;
        if (value !== undefined && value !== null) return JSON.stringify(value);
      }
    } catch {
      return trimmed;
    }
    return trimmed;
  }
  return statusText || fallbackWithStatus;
}

async function taskActionError(
  response: Response,
  fallback: string,
): Promise<Error> {
  const body = await response.text().catch(() => "");
  return new Error(
    extractResponseErrorMessage(
      body,
      response.statusText,
      fallback,
      response.status,
    ),
  );
}

export async function patchTaskFields(
  baseUrl: string,
  taskId: string,
  fields: PatchTaskFields,
): Promise<RawTaskPayload | null> {
  const response = await fetch(
    `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    },
  );
  if (!response.ok) {
    throw await taskActionError(response, "Failed to update task");
  }
  return extractTaskPayload(await response.json());
}

export async function claimTaskForSession(
  baseUrl: string,
  taskId: string,
  sessionId: string,
  force = false,
): Promise<RawTaskPayload | null> {
  const response = await fetch(
    `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/claim`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, force }),
    },
  );
  if (!response.ok) {
    throw await taskActionError(response, "Failed to claim task");
  }
  return extractTaskPayload(await response.json());
}

export async function postTaskLifecycleAction(
  baseUrl: string,
  taskId: string,
  endpoint: TaskActionEndpoint,
  body: Record<string, unknown> = {},
): Promise<RawTaskPayload | null> {
  const response = await fetch(
    `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/${endpoint}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) {
    throw await taskActionError(response, "Task action failed");
  }
  return extractTaskPayload(await response.json());
}
