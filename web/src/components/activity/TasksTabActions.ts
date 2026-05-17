import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalTaskState } from "../../lib/taskState";

export type TaskActionEndpoint = "release-claim" | "close" | "reopen";

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
  await postBuildControl(baseUrl, "stop", task);
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
