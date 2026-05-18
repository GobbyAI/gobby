import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { GobbyTask } from "../../../hooks/useTasks";
import type { RawTaskPayload } from "../../../lib/taskNormalization";
import type { StageStateView } from "../../../lib/stageActions";
import { useTaskStageMove, type StageMovePayload } from "../useTaskStageMove";

type ApplyFn = (taskId: string, raw: RawTaskPayload | null) => void;
type RollbackFn = (taskId: string, snapshot: GobbyTask) => void;
type PatchFn = (
  taskId: string,
  targetStageName: string,
) => Promise<StageMovePayload>;

function stage(name: string, position: number): StageStateView {
  return {
    name,
    display_name: name,
    category: "build",
    state: position === 0 ? "in_progress" : "ready",
    review_policy: "none",
    updated_at: null,
    position,
  };
}

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  const stages = [stage("plan", 0), stage("build", 1), stage("review", 2)];
  return {
    id: "task-1",
    ref: "#14773",
    title: "Board task",
    status: "open",
    state: null,
    compat: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-16T00:00:00Z",
    updated_at: "2026-05-16T00:00:00Z",
    seq_num: 14773,
    path_cache: null,
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    current_stage: stages[0],
    stages,
    ...overrides,
  } as GobbyTask;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useTaskStageMove — optimistic + rollback (#14773 / D6)", () => {
  let applyRawTaskUpdate: Mock<ApplyFn>;
  let rollback: Mock<RollbackFn>;
  let task: GobbyTask;
  const getTask = () => task;

  beforeEach(() => {
    applyRawTaskUpdate = vi.fn<ApplyFn>();
    rollback = vi.fn<RollbackFn>();
    task = makeTask();
  });

  it("applies the optimistic stage move before the PATCH resolves", async () => {
    const gate = deferred<StageMovePayload>();
    const patchMove = vi.fn<PatchFn>().mockReturnValue(gate.promise);
    const { result } = renderHook(() =>
      useTaskStageMove({ patchMove, getTask, applyRawTaskUpdate, rollback }),
    );

    let move!: Promise<void>;
    act(() => {
      move = result.current.moveTaskToStage("task-1", "review");
    });

    expect(patchMove).toHaveBeenCalledWith("task-1", "review");
    const [taskId, optimistic] = applyRawTaskUpdate.mock.calls[0];
    expect(taskId).toBe("task-1");
    expect(optimistic).toMatchObject({ id: "task-1" });
    expect(
      (optimistic as { current_stage: StageStateView }).current_stage.name,
    ).toBe("review");
    expect(result.current.isMovePending("task-1")).toBe(true);

    await act(async () => {
      gate.resolve({ stages: [stage("review", 2)] });
      await move;
    });

    // Reconciled from server truth.
    expect(applyRawTaskUpdate).toHaveBeenLastCalledWith("task-1", {
      id: "task-1",
      stages: [stage("review", 2)],
    });
    expect(result.current.isMovePending("task-1")).toBe(false);
    expect(rollback).not.toHaveBeenCalled();
  });

  it("rolls back and records the error when the move is rejected", async () => {
    const gate = deferred<StageMovePayload>();
    const patchMove = vi.fn<PatchFn>().mockReturnValue(gate.promise);
    const { result } = renderHook(() =>
      useTaskStageMove({ patchMove, getTask, applyRawTaskUpdate, rollback }),
    );

    let move!: Promise<void>;
    act(() => {
      move = result.current.moveTaskToStage("task-1", "review");
    });

    await act(async () => {
      gate.reject(new Error("review not allowed"));
      await expect(move).rejects.toThrow("review not allowed");
    });

    expect(rollback).toHaveBeenCalledWith("task-1", task);
    expect(result.current.errorFor("task-1")).toBe(
      "Couldn't move stage: review not allowed",
    );
  });

  it("is a no-op when the target equals the current stage", async () => {
    const patchMove = vi.fn<PatchFn>();
    const { result } = renderHook(() =>
      useTaskStageMove({ patchMove, getTask, applyRawTaskUpdate, rollback }),
    );

    await act(async () => {
      await result.current.moveTaskToStage("task-1", "plan");
    });

    expect(patchMove).not.toHaveBeenCalled();
    expect(applyRawTaskUpdate).not.toHaveBeenCalled();
  });

  it("does not roll back a stale rejection after reconcile() (WS truth wins)", async () => {
    const gate = deferred<StageMovePayload>();
    const patchMove = vi.fn<PatchFn>().mockReturnValue(gate.promise);
    const { result } = renderHook(() =>
      useTaskStageMove({ patchMove, getTask, applyRawTaskUpdate, rollback }),
    );

    let move!: Promise<void>;
    act(() => {
      move = result.current.moveTaskToStage("task-1", "review");
    });

    // Server truth landed via WS before the slow move settled.
    act(() => {
      result.current.reconcile("task-1");
    });

    await act(async () => {
      gate.reject(new Error("stale"));
      await expect(move).resolves.toBeUndefined();
    });

    expect(rollback).not.toHaveBeenCalled();
    expect(result.current.errorFor("task-1")).toBeNull();
    expect(result.current.isMovePending("task-1")).toBe(false);
  });
});
