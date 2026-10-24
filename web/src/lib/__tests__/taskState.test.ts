import { describe, expect, it } from "vitest";
import {
  TASK_STATE_COLORS,
  countTasksByState,
  getCanonicalTaskState,
  getTaskDisplayState,
  isTaskClosed,
  matchesTaskStateFilter,
} from "../taskState";

describe("taskState helpers", () => {
  it.each(["needs_human", "deferred"] as const)(
    "preserves the canonical %s hold",
    (name) => {
      const task = {
        state: { name, hold_state: name, follow_up_at: "2026-10-23T12:00:00Z" },
      };
      expect(getTaskDisplayState(task)).toBe(name);
      expect(getCanonicalTaskState(task)).toMatchObject({
        name,
        hold_state: name,
        follow_up_at: "2026-10-23T12:00:00Z",
        is_blocked: false,
      });
      expect(matchesTaskStateFilter(task, name)).toBe(true);
      expect(matchesTaskStateFilter(task, "blocked")).toBe(false);
    },
  );

  it("prefers the server's canonical state over incomplete local flags", () => {
    expect(
      getTaskDisplayState({ state: { name: "blocked", is_claimed: true } }),
    ).toBe("blocked");
  });

  it("recognizes closure from a partial canonical payload", () => {
    const task = { state: { name: "closed" as const } };
    expect(isTaskClosed(task)).toBe(true);
    expect(getCanonicalTaskState(task).is_escalated).toBe(false);
    expect(matchesTaskStateFilter(task, "closed")).toBe(true);
  });

  it("recognizes escalation from a partial canonical payload", () => {
    const task = { state: { name: "escalated" as const } };
    expect(getCanonicalTaskState(task).is_escalated).toBe(true);
    expect(isTaskClosed(task)).toBe(false);
    expect(matchesTaskStateFilter(task, "escalated")).toBe(true);
  });

  it("projects a claim when owner details are omitted", () => {
    expect(getTaskDisplayState({ state: { is_claimed: true } })).toBe(
      "in_progress",
    );
  });
  it("exposes the seven generic task states", () => {
    expect(Object.keys(TASK_STATE_COLORS).sort()).toEqual([
      "blocked",
      "closed",
      "deferred",
      "escalated",
      "in_progress",
      "needs_human",
      "open",
    ]);
    expect(TASK_STATE_COLORS.open).not.toBe(TASK_STATE_COLORS.in_progress);
  });

  it("projects task ownership and closure without stage fields", () => {
    const state = getCanonicalTaskState({
      state: { owner_session_id: "sess-1", is_claimed: true, is_closed: false },
    });
    expect(state).toEqual({
      name: "in_progress",
      hold_state: null,
      follow_up_at: null,
      owner_session_id: "sess-1",
      owner_session_ref: null,
      is_claimed: true,
      is_closed: false,
      is_escalated: false,
      is_blocked: false,
      closed_at: null,
      closed_reason: null,
      closed_in_session_id: null,
      closed_commit_sha: null,
      escalated_at: null,
      escalation_reason: null,
    });
  });

  it("uses canonical owner projection when stage rows are missing", () => {
    const state = getCanonicalTaskState({
      state: { owner_session_id: "sess-1", is_claimed: true },
    });

    expect(state.owner_session_id).toBe("sess-1");
    expect(state.is_claimed).toBe(true);
    expect(
      getTaskDisplayState({
        state: { owner_session_id: "sess-1", is_claimed: true },
      }),
    ).toBe("in_progress");
  });

  it("counts tasks by display state", () => {
    expect(
      countTasksByState([
        {},
        { claimed_by_session_id: "sess-1" },
        { state: { is_escalated: true, is_blocked: true } },
        { closed_at: "2026-04-12T00:00:00Z" },
      ]),
    ).toEqual({
      open: 1,
      in_progress: 1,
      blocked: 0,
      needs_human: 0,
      escalated: 1,
      deferred: 0,
      closed: 1,
    });
    const blockedTask = { state: { is_escalated: true, is_blocked: true } };
    expect(matchesTaskStateFilter({}, "open")).toBe(true);
    expect(matchesTaskStateFilter({}, "ready")).toBe(false);
    expect(
      matchesTaskStateFilter(
        { claimed_by_session_id: "sess-1" },
        "in_progress",
      ),
    ).toBe(true);
    expect(matchesTaskStateFilter(blockedTask, "blocked")).toBe(false);
    expect(matchesTaskStateFilter(blockedTask, "escalated")).toBe(true);
    expect(
      matchesTaskStateFilter({ closed_at: "2026-04-12T00:00:00Z" }, "closed"),
    ).toBe(true);
  });
});
