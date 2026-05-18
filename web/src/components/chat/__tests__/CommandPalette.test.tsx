import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CommandPalette } from "../CommandPalette";
import type { GobbySession } from "../../../types/sessions";

function makeSession(overrides: Partial<GobbySession>): GobbySession {
  return {
    id: "session-1",
    ref: "#1",
    external_id: "external-1",
    source: "codex",
    project_id: "proj-1",
    title: "Session",
    status: "active",
    model: "gpt-5.4",
    message_count: 1,
    created_at: "2026-05-04T12:00:00Z",
    updated_at: "2026-05-04T12:01:00Z",
    seq_num: 1,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: false,
    sandbox_policy_hash: null,
    ...overrides,
  };
}

describe("CommandPalette", () => {
  it("only deletes web-chat sessions from the keyboard shortcut", () => {
    Element.prototype.scrollIntoView = vi.fn();
    const onDeleteSession = vi.fn();
    const terminalSession = makeSession({
      id: "terminal-1",
      ref: "#20",
      seq_num: 20,
      session_type: "terminal",
    });
    const webSession = makeSession({
      id: "web-1",
      ref: "#21",
      seq_num: 21,
      session_type: "web_chat",
    });

    render(
      <CommandPalette
        isOpen={true}
        onClose={vi.fn()}
        sessions={[terminalSession, webSession]}
        activeSessionId={null}
        onSelectSession={vi.fn()}
        onDeleteSession={onDeleteSession}
        actions={[]}
      />,
    );

    const input = screen.getByPlaceholderText("Search");
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onDeleteSession).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onDeleteSession).toHaveBeenCalledWith(webSession);
  });
});
