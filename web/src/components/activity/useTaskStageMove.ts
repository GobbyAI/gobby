import { useCallback, useRef, useState } from "react";

import type { GobbyTask } from "../../hooks/useTasks";
import type { RawStagePayload, RawTaskPayload } from "../../lib/taskNormalization";
import { optimisticMoveTaskToStage } from "../../lib/stageActions";

/**
 * D6 — store-agnostic optimistic stage move for the board view.
 *
 * Mirrors the D4 `useTaskInlineEdit` contract: the hook owns no task store.
 * The host (TasksTab) injects snapshot lookup, its own optimistic-apply, a
 * rollback, and the PATCH transport, so there is no second store and no extra
 * fetch. `moveTaskToStage` is the `TasksBoardView` move signature, so the
 * board/`TasksBoardCard` error-tooltip plumbing keeps working (the rejection
 * is re-thrown after rollback).
 */

export interface StageMovePayload {
  /** Server truth: the full stage manifest after the move. */
  stages?: RawStagePayload[] | null;
}

export interface UseTaskStageMoveOptions {
  /**
   * PATCH /api/tasks/{id}/stages/{name} with `{action:'move_to'}`. Throws on
   * non-2xx; resolves to the route's `{stages}` body.
   */
  patchMove: (
    taskId: string,
    targetStageName: string,
  ) => Promise<StageMovePayload>;
  /** Host store snapshot lookup, used for rollback. */
  getTask: (taskId: string) => GobbyTask | null;
  /** Host store optimistic apply (list + detail), reused not duplicated. */
  applyRawTaskUpdate: (taskId: string, raw: RawTaskPayload | null) => void;
  /** Restore the pre-move snapshot on failure. */
  rollback: (taskId: string, snapshot: GobbyTask) => void;
}

export function useTaskStageMove({
  patchMove,
  getTask,
  applyRawTaskUpdate,
  rollback,
}: UseTaskStageMoveOptions) {
  const [errors, setErrors] = useState<Record<string, string | null>>({});
  const [pending, setPending] = useState<Set<string>>(() => new Set());
  // Per-task generation token: bumped by each move and by reconcile() so a
  // slower in-flight move cannot stomp WS truth or a newer move.
  const generationRef = useRef<Map<string, number>>(new Map());

  const bumpGeneration = useCallback((taskId: string): number => {
    const next = (generationRef.current.get(taskId) ?? 0) + 1;
    generationRef.current.set(taskId, next);
    return next;
  }, []);

  const setPendingKey = useCallback((taskId: string, on: boolean) => {
    setPending((prev) => {
      if (on === prev.has(taskId)) return prev;
      const next = new Set(prev);
      if (on) next.add(taskId);
      else next.delete(taskId);
      return next;
    });
  }, []);

  const clearError = useCallback((taskId: string) => {
    setErrors((prev) =>
      prev[taskId] == null ? prev : { ...prev, [taskId]: null },
    );
  }, []);

  /**
   * Called by the host WS task_event handler: server truth landed, so drop
   * our in-flight bookkeeping (a slower move resolve/reject becomes a store
   * no-op) and clear any stale optimistic error.
   */
  const reconcile = useCallback(
    (taskId: string) => {
      bumpGeneration(taskId);
      clearError(taskId);
      setPendingKey(taskId, false);
    },
    [bumpGeneration, clearError, setPendingKey],
  );

  const moveTaskToStage = useCallback(
    async (taskId: string, targetStageName: string): Promise<void> => {
      const task = getTask(taskId);
      if (!task) return;
      if ((task.current_stage?.name ?? null) === targetStageName) return;

      const snapshot = task;
      const generation = bumpGeneration(taskId);

      clearError(taskId);
      setPendingKey(taskId, true);

      const optimistic = optimisticMoveTaskToStage(task, targetStageName);
      const optimisticPayload: RawTaskPayload = {
        id: taskId,
        stages: optimistic.stages,
        current_stage: optimistic.current_stage,
      };
      applyRawTaskUpdate(taskId, optimisticPayload);

      try {
        const payload = await patchMove(taskId, targetStageName);
        if (generationRef.current.get(taskId) !== generation) return;
        if (Array.isArray(payload?.stages)) {
          const serverPayload: RawTaskPayload = {
            id: taskId,
            stages: payload.stages,
          };
          applyRawTaskUpdate(taskId, serverPayload);
        }
      } catch (error) {
        if (generationRef.current.get(taskId) !== generation) return;
        rollback(taskId, snapshot);
        setErrors((prev) => ({
          ...prev,
          [taskId]:
            error instanceof Error
              ? `Couldn't move stage: ${error.message}`
              : "Couldn't move stage.",
        }));
        // Re-throw so the board / card surfaces the transition reason.
        throw error;
      } finally {
        if (generationRef.current.get(taskId) === generation) {
          setPendingKey(taskId, false);
        }
      }
    },
    [
      applyRawTaskUpdate,
      bumpGeneration,
      clearError,
      getTask,
      patchMove,
      rollback,
      setPendingKey,
    ],
  );

  const isMovePending = useCallback(
    (taskId: string): boolean => pending.has(taskId),
    [pending],
  );

  const errorFor = useCallback(
    (taskId: string): string | null => errors[taskId] ?? null,
    [errors],
  );

  return {
    moveTaskToStage,
    reconcile,
    clearError,
    errorFor,
    isMovePending,
    errors,
  };
}
