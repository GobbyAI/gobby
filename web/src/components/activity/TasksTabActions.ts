import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  type RawStagePayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalTaskState } from "../../lib/taskState";

export type TaskActionEndpoint = "release-claim" | "close" | "reopen";

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
    throw new Error(`Failed to update task (${response.status})`);
  }
  return extractTaskPayload(await response.json());
}

/**
 * PATCH /api/tasks/{id}/stages/{name} with `{action:'move_to'}` — the stage
 * move endpoint the board's drag uses. Throws on non-2xx (the message carries
 * the route's transition-reason payload so the board card tooltip can show it);
 * resolves to the route's `{stages}` manifest body on success.
 */
export async function moveTaskStage(
  baseUrl: string,
  taskId: string,
  targetStageName: string,
): Promise<{ stages?: RawStagePayload[] | null }> {
  const response = await fetch(
    `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/stages/${encodeURIComponent(targetStageName)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "move_to" }),
    },
  );
  if (!response.ok) {
    let detail = `Failed to move stage (${response.status})`;
    try {
      const payload = (await response.json()) as Record<string, unknown>;
      const reason =
        (typeof payload.detail === "string" && payload.detail) ||
        (typeof payload.error === "string" && payload.error) ||
        (typeof payload.attempted_transition === "string" &&
          `${payload.attempted_transition} not allowed`);
      if (reason) detail = reason;
    } catch {
      /* keep the status-derived message */
    }
    throw new Error(detail);
  }
  return (await response.json()) as { stages?: RawStagePayload[] | null };
}

export function taskActionRef(task: GobbyTask): string {
  if (task.seq_num != null) return `#${task.seq_num}`;
  return task.ref || task.id;
}

export function currentStageName(task: GobbyTask): string | null {
  return getCanonicalTaskState(task).current_stage?.name ?? task.current_stage?.name ?? null;
}

export async function claimTaskForSession(
  baseUrl: string,
  taskId: string,
  sessionId: string,
): Promise<RawTaskPayload | null> {
  const response = await fetch(
    `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/claim`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, force: true }),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to claim task (${response.status})`);
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
    throw new Error(`Task action failed (${response.status})`);
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
    throw new Error(`Build ${action} failed (${response.status})`);
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
    throw new Error(`Build failed (${response.status})`);
  }
}

async function stopQuickBuildWithRetry(baseUrl: string, task: GobbyTask): Promise<void> {
  const delays = [150, 400, 900];
  let lastError: unknown;
  for (let attempt = 0; attempt <= delays.length; attempt += 1) {
    try {
      await postBuildControl(baseUrl, "stop", task);
      return;
    } catch (error) {
      lastError = error;
      if (attempt === delays.length) break;
      await new Promise((resolve) => setTimeout(resolve, delays[attempt]));
    }
  }
  const ref = taskActionRef(task);
  const detail = lastError instanceof Error ? lastError.message : String(lastError);
  throw new Error(`Quick build started for ${ref}, but stop failed after retries: ${detail}`);
}
