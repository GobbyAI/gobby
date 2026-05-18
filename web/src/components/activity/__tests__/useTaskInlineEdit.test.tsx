import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import type { GobbyTask } from "../../../hooks/useTasks";
import type { RawTaskPayload } from "../../../lib/taskNormalization";
import type { PatchTaskFields } from "../TasksTabActions";
import { useTaskInlineEdit } from "../useTaskInlineEdit";

type ApplyFn = (taskId: string, raw: RawTaskPayload | null) => void;
type RollbackFn = (taskId: string, snapshot: GobbyTask) => void;
type PatchFn = (
  taskId: string,
  fields: PatchTaskFields,
) => Promise<RawTaskPayload | null>;

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  return {
    id: "task-1",
    ref: "#14771",
    title: "Original title",
    status: "open",
    state: null,
    compat: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-16T00:00:00Z",
    updated_at: "2026-05-16T00:00:00Z",
    seq_num: 14771,
    path_cache: null,
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    current_stage: null,
    stages: [],
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

describe("useTaskInlineEdit — optimistic + rollback (#14771 / D4)", () => {
  let applyRawTaskUpdate: Mock<ApplyFn>;
  let rollback: Mock<RollbackFn>;

  beforeEach(() => {
    applyRawTaskUpdate = vi.fn<ApplyFn>();
    rollback = vi.fn<RollbackFn>();
  });

  it("applies the optimistic value before the PATCH resolves", async () => {
    const gate = deferred<RawTaskPayload | null>();
    const patchTask = vi.fn<PatchFn>().mockReturnValue(gate.promise);
    const { result } = renderHook(() =>
      useTaskInlineEdit({ patchTask, applyRawTaskUpdate, rollback }),
    );

    let commit!: Promise<void>;
    act(() => {
      commit = result.current.commitField({
        task: makeTask(),
        field: "title",
        value: "New title",
      });
    });

    // Optimistic apply happened synchronously, before the server answered.
    expect(applyRawTaskUpdate).toHaveBeenCalledWith("task-1", {
      id: "task-1",
      title: "New title",
    });
    expect(patchTask).toHaveBeenCalledWith("task-1", { title: "New title" });
    expect(result.current.isFieldPending("task-1", "title")).toBe(true);

    await act(async () => {
      gate.resolve({ id: "task-1", title: "New title" } as RawTaskPayload);
      await commit;
    });

    expect(applyRawTaskUpdate).toHaveBeenLastCalledWith("task-1", {
      id: "task-1",
      title: "New title",
    });
    expect(rollback).not.toHaveBeenCalled();
    expect(result.current.errorFor("task-1")).toBeNull();
    expect(result.current.isFieldPending("task-1", "title")).toBe(false);
  });

  it("rolls back to the snapshot and surfaces an inline error on failure", async () => {
    const patchTask = vi
      .fn<PatchFn>()
      .mockRejectedValue(new Error("Failed to update task (500)"));
    const snapshot = makeTask();
    const { result } = renderHook(() =>
      useTaskInlineEdit({ patchTask, applyRawTaskUpdate, rollback }),
    );

    await act(async () => {
      await result.current.commitField({
        task: snapshot,
        field: "description",
        value: "edited",
      });
    });

    expect(rollback).toHaveBeenCalledWith("task-1", snapshot);
    expect(result.current.errorFor("task-1")).toBe(
      "Couldn't save description: Failed to update task (500)",
    );
    expect(result.current.isFieldPending("task-1", "description")).toBe(false);
  });

  it("does nothing when the value is unchanged", async () => {
    const patchTask = vi.fn<PatchFn>();
    const { result } = renderHook(() =>
      useTaskInlineEdit({ patchTask, applyRawTaskUpdate, rollback }),
    );

    await act(async () => {
      await result.current.commitField({
        task: makeTask({ title: "Same" }),
        field: "title",
        value: "Same",
      });
    });

    expect(patchTask).not.toHaveBeenCalled();
    expect(applyRawTaskUpdate).not.toHaveBeenCalled();
  });

  it("never PATCHes a non-patch field (assignee guard)", async () => {
    const patchTask = vi.fn<PatchFn>();
    const { result } = renderHook(() =>
      useTaskInlineEdit({ patchTask, applyRawTaskUpdate, rollback }),
    );

    await act(async () => {
      await result.current.commitField({
        task: makeTask(),
        // @ts-expect-error — assignee is intentionally not a PatchEditableField
        field: "assignee",
        value: "session-9",
      });
    });

    expect(patchTask).not.toHaveBeenCalled();
  });

  it("a WS reconcile during an in-flight request suppresses a stale rollback", async () => {
    const gate = deferred<RawTaskPayload | null>();
    const patchTask = vi.fn<PatchFn>().mockReturnValue(gate.promise);
    const { result } = renderHook(() =>
      useTaskInlineEdit({ patchTask, applyRawTaskUpdate, rollback }),
    );

    let commit!: Promise<void>;
    act(() => {
      commit = result.current.commitField({
        task: makeTask(),
        field: "title",
        value: "Optimistic",
      });
    });

    // WS task_event lands first; host applied server truth and called reconcile.
    act(() => {
      result.current.reconcile("task-1");
    });

    await act(async () => {
      gate.reject(new Error("late failure"));
      await commit;
    });

    // The slow rejection must not stomp WS truth.
    expect(rollback).not.toHaveBeenCalled();
    expect(result.current.errorFor("task-1")).toBeNull();
    expect(result.current.isFieldPending("task-1", "title")).toBe(false);
  });
});
