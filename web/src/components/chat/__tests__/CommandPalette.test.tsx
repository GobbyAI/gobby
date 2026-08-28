import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    handoff_markdown: null,
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
    const originalScrollIntoView = Element.prototype.scrollIntoView;
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
    const onDeleteSession = vi.fn();
    // Sessions render in #N-descending order, so the terminal session (#21)
    // sits at index 0 and the web-chat session (#20) at index 1.
    const terminalSession = makeSession({
      id: "terminal-1",
      ref: "#21",
      seq_num: 21,
      session_type: "terminal",
    });
    const webSession = makeSession({
      id: "web-1",
      ref: "#20",
      seq_num: 20,
      session_type: "web_chat",
    });

    try {
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
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(Element.prototype, "scrollIntoView", {
          configurable: true,
          value: originalScrollIntoView,
        });
      } else {
        delete (Element.prototype as { scrollIntoView?: unknown })
          .scrollIntoView;
      }
    }
  });

  it("orders sessions by ref (#N) descending", () => {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
    const sessions = [
      makeSession({ id: "a", ref: "#5", seq_num: 5 }),
      makeSession({ id: "b", ref: "#42", seq_num: 42 }),
      makeSession({ id: "c", ref: "#13", seq_num: 13 }),
    ];

    render(
      <CommandPalette
        isOpen={true}
        onClose={vi.fn()}
        sessions={sessions}
        activeSessionId={null}
        onSelectSession={vi.fn()}
        onDeleteSession={vi.fn()}
        actions={[]}
      />,
    );

    const refs = screen
      .getAllByTestId("session-ref")
      .map((el) => el.textContent);
    expect(refs).toEqual(["#42", "#13", "#5"]);
  });

  it("targets the visually highlighted session across recency buckets", () => {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
    const now = Date.now();
    const todaySession = makeSession({
      id: "today",
      ref: "#1",
      seq_num: 1,
      updated_at: new Date(now - 60_000).toISOString(),
    });
    const weekSession = makeSession({
      id: "week",
      ref: "#30",
      seq_num: 30,
      updated_at: new Date(now - 2 * 86_400_000).toISOString(),
    });
    const olderSession = makeSession({
      id: "older",
      ref: "#20",
      seq_num: 20,
      updated_at: new Date(now - 8 * 86_400_000).toISOString(),
    });
    const onSelectSession = vi.fn();
    const onDeleteSession = vi.fn();

    render(
      <CommandPalette
        isOpen={true}
        onClose={vi.fn()}
        sessions={[weekSession, olderSession, todaySession]}
        activeSessionId={null}
        onSelectSession={onSelectSession}
        onDeleteSession={onDeleteSession}
        actions={[]}
      />,
    );

    const input = screen.getByPlaceholderText("Search");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Backspace" });

    expect(
      screen.getAllByTestId("session-ref").map((el) => el.textContent),
    ).toEqual(["#1", "#30", "#20"]);
    expect(onSelectSession).toHaveBeenCalledWith(todaySession);
    expect(onDeleteSession).toHaveBeenCalledWith(todaySession);
  });

  it("exposes listbox state, traps Tab, and restores focus to the invoker", async () => {
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: vi.fn(),
    });
    const invoker = document.createElement("button");
    document.body.append(invoker);
    invoker.focus();
    const session = makeSession({ id: "session-a", seq_num: 2 });
    const action = {
      id: "action-a",
      label: "Open settings",
      category: "navigate" as const,
      onSelect: vi.fn(),
    };
    const props = {
      onClose: vi.fn(),
      sessions: [session],
      activeSessionId: null,
      onSelectSession: vi.fn(),
      actions: [action],
    };

    const { rerender } = render(<CommandPalette {...props} isOpen={true} />);
    const dialog = screen.getByRole("dialog", { name: "Command palette" });
    const combobox = screen.getByRole("combobox");
    const listbox = screen.getByRole("listbox", {
      name: "Command palette results",
    });
    const options = screen.getAllByRole("option");

    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(combobox).toHaveAttribute("aria-controls", listbox.id);
    expect(combobox).toHaveAttribute("aria-activedescendant", options[0].id);
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(combobox, { key: "ArrowDown" });

    expect(combobox).toHaveAttribute("aria-activedescendant", options[1].id);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(combobox).toHaveFocus());
    expect(fireEvent.keyDown(combobox, { key: "Tab" })).toBe(false);
    expect(combobox).toHaveFocus();

    rerender(<CommandPalette {...props} isOpen={false} />);

    await waitFor(() => expect(invoker).toHaveFocus());
    invoker.remove();
  });
});
