import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  normalizeTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalTaskState } from "../../lib/taskState";

export function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

export { extractTaskPayload };

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
  return getCanonicalTaskState(task).current_stage?.name ?? task.current_stage?.name ?? null;
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
    operation: "fetch" | "extractTaskPayload" | "normalizeActivityTask" | "enqueueParent",
    error: unknown,
  ) => {
    console.error("Failed to fetch task ancestor", { parentId, operation, error });
  };

  for (const task of seedTasks) {
    try {
      enqueueParent(task);
    } catch (error) {
      logAncestorFailure(task.parent_task_id ?? task.id, "enqueueParent", error);
    }
  }

  while (parentQueue.length > 0) {
    const parentId = parentQueue.shift();
    if (!parentId || signal.aborted) break;
    queuedParentIds.delete(parentId);

    try {
      let response: Response;
      try {
        response = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(parentId)}`,
          { signal },
        );
      } catch (error) {
        if (error instanceof Error && error.name === "AbortError") throw error;
        logAncestorFailure(parentId, "fetch", error);
        continue;
      }
      if (!response.ok) continue;
      const data = await response.json();
      const raw = extractTaskPayload(data);
      if (!raw) {
        logAncestorFailure(parentId, "extractTaskPayload", data);
        continue;
      }
      let parentTask: GobbyTask;
      try {
        parentTask = normalizeActivityTask(raw, taskMap.get(parentId) ?? null);
      } catch (error) {
        logAncestorFailure(parentId, "normalizeActivityTask", error);
        continue;
      }
      taskMap.set(parentTask.id, parentTask);
      try {
        enqueueParent(parentTask);
      } catch (error) {
        logAncestorFailure(parentId, "enqueueParent", error);
      }
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      logAncestorFailure(parentId, "fetch", err);
    }
  }

  return [...taskMap.values()];
}
