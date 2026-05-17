import { useCallback, useRef, useState } from "react";

import type { GobbyTask } from "../../hooks/useTasks";
import type { RawTaskPayload } from "../../lib/taskNormalization";
import {
  isPatchEditableField,
  type PatchEditableField,
} from "./taskFieldRouting";
import type { PatchTaskFields } from "./TasksTabActions";

/**
 * D4 — store-agnostic optimistic inline editor for PATCH-family task fields.
 *
 * The hook owns no task store. The host (TasksTab) injects its own
 * optimistic-apply, rollback, and PATCH transport, so there is no second
 * store and no extra fetch. Only PATCH-family fields ever reach `patchTask`
 * (assignee/state/stage/terminal route elsewhere — see taskFieldRouting),
 * so the PATCH-400-on-assignee path is structurally unreachable from here.
 */

export type PatchFieldValue = string | number | string[];

export interface UseTaskInlineEditOptions {
  /** PATCH /api/tasks/{id}. Throws on non-2xx; resolves to the server task. */
  patchTask: (
    taskId: string,
    fields: PatchTaskFields,
  ) => Promise<RawTaskPayload | null>;
  /** Host store's optimistic apply (list + detail), reused not duplicated. */
  applyRawTaskUpdate: (taskId: string, raw: RawTaskPayload | null) => void;
  /** Restore the pre-edit task snapshot on failure. */
  rollback: (taskId: string, snapshot: GobbyTask) => void;
}

export interface CommitFieldArgs {
  task: GobbyTask;
  field: PatchEditableField;
  value: PatchFieldValue;
}

type EditableFieldMap = {
  [K in PatchEditableField]: PatchFieldValue | null | undefined;
};

function currentFieldValue(
  task: GobbyTask & Partial<EditableFieldMap>,
  field: PatchEditableField,
): PatchFieldValue | null | undefined {
  return task[field];
}

function sameValue(
  a: PatchFieldValue | null | undefined,
  b: PatchFieldValue,
): boolean {
  const aIsArray = Array.isArray(a);
  const bIsArray = Array.isArray(b);
  if (aIsArray || bIsArray) {
    if (!aIsArray || !bIsArray) return false;
    return a.length === b.length && a.every((item, index) => item === b[index]);
  }
  return a === b;
}

export function useTaskInlineEdit({
  patchTask,
  applyRawTaskUpdate,
  rollback,
}: UseTaskInlineEditOptions) {
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [pending, setPending] = useState<Set<string>>(() => new Set());
  // Per-task generation token: bumped by each commit and by reconcile() so a
  // slower in-flight request cannot stomp WS truth or a newer edit.
  const generationRef = useRef<Map<string, number>>(new Map());

  const bumpGeneration = useCallback((taskId: string): number => {
    const next = (generationRef.current.get(taskId) ?? 0) + 1;
    generationRef.current.set(taskId, next);
    return next;
  }, []);

  const setPendingKey = useCallback((key: string, on: boolean) => {
    setPending((prev) => {
      if (on === prev.has(key)) return prev;
      const next = new Set(prev);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }, []);

  const clearError = useCallback((taskId: string) => {
    setErrors((prev) =>
      prev[taskId] == null ? prev : { ...prev, [taskId]: null },
    );
  }, []);

  /**
   * Called by the host WS task_event handler. The host already applied
   * server truth, so drop our in-flight bookkeeping: bump the generation
   * (a slower PATCH resolve/reject becomes a store no-op) and clear any
   * stale optimistic error — WS truth supersedes it.
   */
  const reconcile = useCallback(
    (taskId: string) => {
      bumpGeneration(taskId);
      clearError(taskId);
    },
    [bumpGeneration, clearError],
  );

  const commitField = useCallback(
    async ({ task, field, value }: CommitFieldArgs): Promise<void> => {
      // Defense in depth: only PATCH-family fields reach patchTask.
      if (!isPatchEditableField(field)) return;
      if (sameValue(currentFieldValue(task, field), value)) return;

      const taskId = task.id;
      const pendingKey = `${taskId}:${field}`;
      const snapshot = task;
      const generation = bumpGeneration(taskId);

      clearError(taskId);
      setPendingKey(pendingKey, true);
      applyRawTaskUpdate(taskId, {
        id: taskId,
        [field]: value,
      } as RawTaskPayload);

      try {
        const serverRaw = await patchTask(taskId, {
          [field]: value,
        } as PatchTaskFields);
        if (generationRef.current.get(taskId) !== generation) return;
        applyRawTaskUpdate(taskId, serverRaw);
      } catch (error) {
        if (generationRef.current.get(taskId) !== generation) return;
        rollback(taskId, snapshot);
        setErrors((prev) => ({
          ...prev,
          [taskId]:
            error instanceof Error
              ? `Couldn't save ${field}: ${error.message}`
              : `Couldn't save ${field}.`,
        }));
      } finally {
        setPendingKey(pendingKey, false);
      }
    },
    [
      applyRawTaskUpdate,
      bumpGeneration,
      clearError,
      patchTask,
      rollback,
      setPendingKey,
    ],
  );

  const isFieldPending = useCallback(
    (taskId: string, field: string): boolean =>
      pending.has(`${taskId}:${field}`),
    [pending],
  );

  const errorFor = useCallback(
    (taskId: string): string | null => errors[taskId] ?? null,
    [errors],
  );

  return {
    commitField,
    reconcile,
    clearError,
    errorFor,
    isFieldPending,
    errors,
  };
}
