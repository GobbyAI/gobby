import type { GobbyTask } from "../../hooks/useTasks";
import {
  extractTaskPayload,
  normalizeTaskPayload,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import { getCanonicalStageName } from "../../lib/taskState";

export function getBaseUrl(): string {
  // Empty means same-origin; production serves the UI and API from one daemon origin.
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

export async function fetchMissingTaskAncestors(
  baseUrl: string,
  seedTasks: GobbyTask[],
  signal: AbortSignal,
  maxDepth = 50,
): Promise<GobbyTask[]> {
  const taskMap = new Map(seedTasks.map((task) => [task.id, task]));
  const queuedParentIds = new Set<string>();
  const attemptedParentIds = new Set<string>();
  const parentQueue: Array<{ parentId: string; depth: number }> = [];

  const enqueueParent = (task: GobbyTask, depth: number) => {
    const parentId = task.parent_task_id;
    if (
      !parentId ||
      depth >= maxDepth ||
      taskMap.has(parentId) ||
      queuedParentIds.has(parentId) ||
      attemptedParentIds.has(parentId)
    ) {
      return;
    }
    queuedParentIds.add(parentId);
    parentQueue.push({ parentId, depth: depth + 1 });
  };

  for (const task of seedTasks) {
    enqueueParent(task, 0);
  }

  while (parentQueue.length > 0) {
    const item = parentQueue.shift();
    if (!item) continue;
    const { parentId, depth } = item;
    queuedParentIds.delete(parentId);
    attemptedParentIds.add(parentId);

    try {
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(parentId)}`,
        { signal },
      );
      if (!response.ok) {
        console.warn("Failed to fetch missing task parent", {
          parentId,
          status: response.status,
        });
        continue;
      }

      const data = await response.json();

      const raw = extractApiTaskResponse(data);
      if (!raw) {
        console.warn("Failed to normalize missing task parent payload", {
          parentId,
        });
        continue;
      }

      const parentTask = normalizeActivityTask(raw, taskMap.get(parentId) ?? null);
      taskMap.set(parentTask.id, parentTask);

      enqueueParent(parentTask, depth);
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      console.warn("Failed to load missing task parent", { parentId, error: err });
    }
  }

  return [...taskMap.values()];
}
