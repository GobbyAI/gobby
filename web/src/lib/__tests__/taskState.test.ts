import { describe, expect, it } from "vitest";
import {
  TASK_STATE_COLORS,
  countTasksByState,
  getCanonicalTaskState,
  getTaskDisplayState,
  matchesTaskStateFilter,
} from "../taskState";

describe("taskState helpers", () => {
  it("exposes only claim-based display states", () => {
    expect(Object.keys(TASK_STATE_COLORS).sort()).toEqual([
      "blocked",
      "closed",
      "in_progress",
      "ready",
    ]);
    expect(TASK_STATE_COLORS.ready).not.toBe(TASK_STATE_COLORS.in_progress);
  });

  it("projects task ownership and closure without stage fields", () => {
    const state = getCanonicalTaskState({
      state: { owner_session_id: "sess-1", is_claimed: true, is_closed: false },
    });
    expect(state).toEqual({
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
      ready: 1,
      in_progress: 1,
      blocked: 1,
      closed: 1,
    });
    const blockedTask = { state: { is_escalated: true, is_blocked: true } };
    expect(matchesTaskStateFilter({}, "ready")).toBe(true);
    expect(
      matchesTaskStateFilter(
        { claimed_by_session_id: "sess-1" },
        "in_progress",
      ),
    ).toBe(true);
    expect(matchesTaskStateFilter(blockedTask, "blocked")).toBe(true);
    expect(matchesTaskStateFilter(blockedTask, "escalated")).toBe(true);
    expect(
      matchesTaskStateFilter({ closed_at: "2026-04-12T00:00:00Z" }, "closed"),
    ).toBe(true);
  });
});
