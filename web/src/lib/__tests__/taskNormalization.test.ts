import { describe, expect, it } from "vitest";
import {
  extractTaskPayload,
  isRawTaskPayload,
  normalizeTaskPayload,
} from "../taskNormalization";

describe("isRawTaskPayload optional field guard", () => {
  it("accepts string, string array, and null optional task fields", () => {
    expect(
      isRawTaskPayload({
        id: "task-1",
        description: "Describe the task",
        validation_criteria: null,
        labels: ["web", "tasks"],
        additional_skills: null,
      }),
    ).toBe(true);
  });

  it("rejects invalid optional task field values", () => {
    const invalidPayloads = [
      { id: "task-1", description: 42 },
      { id: "task-1", validation_criteria: ["run tests"] },
      { id: "task-1", labels: "web" },
      { id: "task-1", labels: ["web", 42] },
      { id: "task-1", owner_session_ref: { session_id: "sess-1" } },
      { id: "task-1", owner_session_ref: { session_id: "", ref: "#1" } },
      { id: "task-1", owner_session_ref: { session_id: "sess-1", ref: " " } },
    ];

    invalidPayloads.forEach((payload) => {
      expect(isRawTaskPayload(payload)).toBe(false);
    });
  });

  it("accepts resolved owner session refs", () => {
    expect(
      isRawTaskPayload({
        id: "task-1",
        owner_session_ref: {
          session_id: "sess-1",
          ref: "#12",
          source: "codex",
        },
      }),
    ).toBe(true);
  });

  it("accepts resolved owner session refs without source", () => {
    expect(
      isRawTaskPayload({
        id: "task-1",
        owner_session_ref: {
          session_id: "sess-1",
          ref: "#12",
        },
      }),
    ).toBe(true);
  });
});

describe("extractTaskPayload", () => {
  it("extracts nested task payloads from data wrappers", () => {
    expect(
      extractTaskPayload({ data: { data: { task: { id: "task-1" } } } }),
    ).toEqual({
      id: "task-1",
    });
  });

  it("caps nested payload extraction depth", () => {
    expect(
      extractTaskPayload({
        data: {
          data: { data: { data: { data: { task: { id: "task-1" } } } } },
        },
      }),
    ).toBeNull();
  });

  it("extracts payloads before the depth guard boundary", () => {
    expect(
      extractTaskPayload({
        data: { data: { data: { data: { task: { id: "task-1" } } } } },
      }),
    ).toEqual({ id: "task-1" });
  });

  it("returns null for circular wrapper payloads", () => {
    const payload: Record<string, unknown> = {};
    payload.data = payload;
    expect(extractTaskPayload(payload)).toBeNull();
  });
});

describe("normalizeTaskPayload", () => {
  it("fills display defaults for a ready task", () => {
    const task = normalizeTaskPayload({ id: "task-1", seq_num: 42 });
    expect(task).toMatchObject({
      id: "task-1",
      ref: "#42",
      title: "",
      status: "ready",
      task_type: "task",
      parent_task_id: null,
      state: { is_claimed: false, is_closed: false, is_blocked: false },
    });
  });

  it("preserves canonical ownership and derives claimed status", () => {
    const task = normalizeTaskPayload({
      id: "task-1",
      state: {
        owner_session_id: "session-1",
        is_claimed: true,
        is_closed: false,
      },
    });
    expect(task.status).toBe("in_progress");
    expect(task.state.owner_session_id).toBe("session-1");
  });

  it("projects closure from its timestamp", () => {
    const task = normalizeTaskPayload({
      id: "task-1",
      closed_at: "2026-10-21T12:00:00Z",
    });
    expect(task.status).toBe("closed");
    expect(task.state).toMatchObject({
      is_closed: true,
      is_claimed: false,
      closed_at: "2026-10-21T12:00:00Z",
    });
  });
});
