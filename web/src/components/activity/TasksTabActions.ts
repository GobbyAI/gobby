import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalStageName } from "../../lib/taskState";

export type TaskActionEndpoint = "release-claim" | "close" | "reopen";
export const QUICK_BUILD_STOP_RETRY_DELAYS_MS = [150, 400, 900] as const;
export const QUICK_BUILD_STOP_RETRY_JITTER_MS = 75;

/**
 * PATCH-safe task fields only. Mirrors the backend TaskUpdateRequest subset
 * that the PATCH route accepts without 400ing (it rejects `assignee`,
 * `status`, and stage keys — those route to dedicated endpoints instead).
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

class TaskActionHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: string,
  ) {
    super(message);
  }
}

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

async function taskActionError(response: Response, fallback: string): Promise<TaskActionHttpError> {
  const body = await response.text().catch(() => "");
  return new TaskActionHttpError(
    extractResponseErrorMessage(body, response.statusText, fallback, response.status),
    response.status,
    body,
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

export function taskActionRef(task: GobbyTask): string {
  if (task.seq_num != null) return `#${task.seq_num}`;
  return task.ref || task.id;
}

export function currentStageName(task: GobbyTask): string | null {
  return getCanonicalStageName(task);
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

export async function startBuild(baseUrl: string, task: GobbyTask): Promise<void> {
  await postBuildRequest(baseUrl, {
    input_ref: taskActionRef(task),
  });
}

export async function startQuickBuild(baseUrl: string, task: GobbyTask): Promise<void> {
  const stageName = currentStageName(task);
  await postBuildRequest(baseUrl, {
    input_ref: taskActionRef(task),
    quick: true,
    stage: stageName ? [stageName] : [],
  });
  // Quick build intentionally seeds automation, then pauses it so the user can inspect.
  await stopQuickBuildWithRetry(baseUrl, task);
}

export async function postBuildControl(
  baseUrl: string,
  action: "stop" | "resume",
  task: GobbyTask,
): Promise<void> {
  const response = await fetch(`${baseUrl}/api/build/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input_ref: taskActionRef(task) }),
  });
  if (!response.ok) {
    throw await taskActionError(response, `Build ${action} failed`);
  }
}

async function postBuildRequest(
  baseUrl: string,
  body: Record<string, unknown>,
): Promise<void> {
  const response = await fetch(`${baseUrl}/api/build`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await taskActionError(response, "Build failed");
  }
}

function isSemanticStopError(error: unknown): boolean {
  if (error instanceof TaskActionHttpError && [404, 409, 410].includes(error.status)) {
    return true;
  }
  const message = error instanceof Error ? error.message : String(error);
  return /\bnot[- ]running\b|\bno running build\b/i.test(message);
}

export function buildRetryDelay(baseDelay: number): number {
  return baseDelay + Math.floor(Math.random() * QUICK_BUILD_STOP_RETRY_JITTER_MS);
}

async function stopQuickBuildWithRetry(baseUrl: string, task: GobbyTask): Promise<void> {
  // Attempts are the initial stop call plus one retry for each configured delay.
  const maxAttempts = QUICK_BUILD_STOP_RETRY_DELAYS_MS.length + 1;
  let lastError: unknown;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      await postBuildControl(baseUrl, "stop", task);
      return;
    } catch (error) {
      lastError = error;
      if (isSemanticStopError(error)) {
        console.info("Quick build stop skipped; build is no longer running", {
          taskId: task.id,
          error,
        });
        return;
      }
      const delay = QUICK_BUILD_STOP_RETRY_DELAYS_MS[attempt];
      if (delay === undefined) break;
      console.warn("Quick build stop failed transiently; retrying", {
        taskId: task.id,
        attempt: attempt + 1,
        error,
      });
      await new Promise((resolve) => setTimeout(resolve, buildRetryDelay(delay)));
    }
  }
  const ref = taskActionRef(task);
  const detail = lastError instanceof Error ? lastError.message : String(lastError);
  throw new Error(`Quick build started for ${ref}, but stop failed after retries: ${detail}`);
}
