import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  normalizeTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalStageName } from "../../lib/taskState";

export function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

export { extractTaskPayload };

export type ApiTaskResponse = RawTaskPayload | { task: RawTaskPayload };

export function isApiTaskResponse(data: unknown): data is ApiTaskResponse {
  return extractTaskPayload(data) !== null;
}

export function extractApiTaskResponse(data: unknown): RawTaskPayload | null {
  return extractTaskPayload(data);
}

export function normalizeActivityTask(
  raw: RawTaskPayload,
  fallback?: GobbyTask | null,
): GobbyTask {
  return normalizeTaskPayload({
    ...fallback,
    ...raw,
    stages: raw.stages ?? fallback?.stages ?? [],
    current_stage:
      raw.current_stage ??
      raw.state?.current_stage ??
      fallback?.current_stage ??
      fallback?.state?.current_stage ??
      null,
  }) as GobbyTask;
}

export function areSetsEqual<T>(left: ReadonlySet<T>, right: ReadonlySet<T>): boolean {
  if (left.size !== right.size) return false;
  for (const value of left) {
    if (!right.has(value)) return false;
  }
  return true;
}

export function getCurrentStageName(task: GobbyTask): string | null {
  return getCanonicalStageName(task);
}

export function mergeTasksById(...taskGroups: GobbyTask[][]): GobbyTask[] {
  const taskMap = new Map<string, GobbyTask>();
  for (const group of taskGroups) {
    for (const task of group) {
      taskMap.set(task.id, task);
    }
  }
  return [...taskMap.values()];
}

type AncestorOperation =
  | "fetch"
  | "parse"
  | "extractTaskPayload"
  | "normalizeActivityTask"
  | "enqueueParent";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function sanitizeTaskForLogging(task: unknown): Record<string, unknown> {
  const source = isRecord(task) && isRecord(task.task) ? task.task : task;
  if (!isRecord(source)) return { payloadType: typeof task };

  const safeKeys = [
    "id",
    "ref",
    "status",
    "task_type",
    "parent_task_id",
    "created_at",
    "updated_at",
    "project_id",
  ] as const;
  return Object.fromEntries(
    safeKeys.flatMap((key) => (source[key] === undefined ? [] : [[key, source[key]]])),
  );
}

export async function fetchMissingTaskAncestors(
  baseUrl: string,
  seedTasks: GobbyTask[],
  signal: AbortSignal,
): Promise<GobbyTask[]> {
  const taskMap = new Map(seedTasks.map((task) => [task.id, task]));
  const queuedParentIds = new Set<string>();
  const parentQueue: string[] = [];

  const enqueueParent = (task: GobbyTask) => {
    const parentId = task.parent_task_id;
    if (!parentId || taskMap.has(parentId) || queuedParentIds.has(parentId)) {
      return;
    }
    queuedParentIds.add(parentId);
    parentQueue.push(parentId);
  };

  const logAncestorFailure = (
    parentId: string,
    operation: AncestorOperation,
    error: unknown,
  ) => {
    console.error("Failed to fetch task ancestor", { parentId, operation, error });
  };

  for (const task of seedTasks) {
    enqueueParent(task);
  }

  while (parentQueue.length > 0) {
    const parentId = parentQueue.shift();
    if (!parentId || signal.aborted) break;
    queuedParentIds.delete(parentId);

    let operation: AncestorOperation = "fetch";
    try {
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(parentId)}`,
        { signal },
      );
      if (!response.ok) {
        console.error("Failed to fetch task ancestor", {
          parentId,
          operation,
          status: response.status,
        });
        continue;
      }

      operation = "parse";
      const data = await response.json();

      operation = "extractTaskPayload";
      const raw = extractApiTaskResponse(data);
      if (!raw) {
        logAncestorFailure(parentId, "extractTaskPayload", sanitizeTaskForLogging(data));
        continue;
      }

      operation = "normalizeActivityTask";
      const parentTask = normalizeActivityTask(raw, taskMap.get(parentId) ?? null);
      taskMap.set(parentTask.id, parentTask);

      operation = "enqueueParent";
      enqueueParent(parentTask);
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      logAncestorFailure(parentId, operation, err);
    }
  }

  return [...taskMap.values()];
}
