import { describe, expect, it } from "vitest";

import { getVisibleActivitySessions } from "../activitySessionVisibility";
import type { GobbySession } from "../../../types/sessions";

function makeSession(
  overrides: Partial<GobbySession> = {},
): GobbySession {
  return {
    id: "session-1",
    ref: "#1",
    external_id: "external-1",
    source: "codex",
    project_id: "project-1",
    title: "Session",
    status: "active",
    model: "gpt-5.4",
    message_count: 0,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-01T00:00:00Z",
    seq_num: 1,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: null,
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
    ...overrides,
  };
}

describe("getVisibleActivitySessions", () => {
  it("sorts without mutating the caller-owned session array", () => {
    const first = makeSession({ id: "old", ref: "#1", seq_num: 1 });
    const second = makeSession({ id: "new", ref: "#2", seq_num: 2 });
    const sessions = [first, second];

    const visible = getVisibleActivitySessions(sessions);

    expect(visible.map((session) => session.id)).toEqual(["new", "old"]);
    expect(sessions.map((session) => session.id)).toEqual(["old", "new"]);
  });

  it("places missing and invalid timestamps after valid timestamps", () => {
    const valid = makeSession({
      id: "valid",
      ref: "#1",
      seq_num: null,
      created_at: "2026-05-01T00:00:00Z",
    });
    const missing = makeSession({
      id: "missing",
      ref: "#2",
      seq_num: null,
      created_at: "",
    });
    const invalid = makeSession({
      id: "invalid",
      ref: "#3",
      seq_num: null,
      created_at: "not-a-date",
    });

    expect(
      getVisibleActivitySessions([missing, valid, invalid]).map(
        (session) => session.id,
      ),
    ).toEqual(["valid", "missing", "invalid"]);
  });

  it("uses the live status allowlist when liveOnly is enabled", () => {
    const visible = getVisibleActivitySessions(
      [
        makeSession({ id: "active", status: "active" }),
        makeSession({ id: "paused", status: "paused" }),
        makeSession({ id: "expired", status: "expired" }),
      ],
      { liveOnly: true },
    );

    expect(visible.map((session) => session.id)).toEqual(["active", "paused"]);
  });
});
