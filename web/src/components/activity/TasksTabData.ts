import type { GobbyTask } from "../../hooks/useTasks";
import { normalizeTaskPayload, type RawTaskPayload } from "../../lib/taskNormalization";
import { getCanonicalTaskState } from "../../lib/taskState";

export function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

export function extractTaskPayload(data: unknown): RawTaskPayload | null {
  if (!data || typeof data !== "object") return null;
  const record = data as { id?: unknown; task?: unknown };
  if (typeof record.id === "string") return record as RawTaskPayload;
  if (record.task && typeof record.task === "object") {
    return record.task as RawTaskPayload;
  }
  return null;
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

  seedTasks.forEach(enqueueParent);

  while (parentQueue.length > 0) {
    const parentId = parentQueue.shift();
    if (!parentId || signal.aborted) break;
    queuedParentIds.delete(parentId);

    try {
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(parentId)}`,
        { signal },
      );
      if (!response.ok) continue;
      const raw = extractTaskPayload(await response.json());
      if (!raw) continue;
      const parentTask = normalizeActivityTask(raw, taskMap.get(parentId) ?? null);
      taskMap.set(parentTask.id, parentTask);
      enqueueParent(parentTask);
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
    }
  }

  return [...taskMap.values()];
}
