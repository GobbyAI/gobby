import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import type { TmuxSession } from "../../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../../types/sessions";
import { TerminalSessionPicker } from "../TerminalSessionPicker";
import {
  type JoinedTerminalSession,
  sessionKey,
} from "../terminalSessions";

const originalElementMethods = {
  hasPointerCapture: Element.prototype.hasPointerCapture,
  setPointerCapture: Element.prototype.setPointerCapture,
  releasePointerCapture: Element.prototype.releasePointerCapture,
  scrollIntoView: Element.prototype.scrollIntoView,
};

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => undefined;
  Element.prototype.releasePointerCapture = () => undefined;
  Element.prototype.scrollIntoView = () => undefined;
});

afterAll(() => {
  for (const [name, method] of Object.entries(originalElementMethods)) {
    if (typeof method === "function") {
      Object.defineProperty(Element.prototype, name, {
        configurable: true,
        writable: true,
        value: method,
      });
    } else {
      Reflect.deleteProperty(Element.prototype, name);
    }
  }
});

function makeGobbySession(overrides: Partial<GobbySession> = {}): GobbySession {
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

function makeTmuxSession(overrides: Partial<TmuxSession> = {}): TmuxSession {
  return {
    name: "shell",
    socket: "default",
    pane_pid: 123,
    pane_dead: false,
    pane_title: null,
    window_name: null,
    session_title: null,
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
    ...overrides,
  };
}

function joinedSessions(): JoinedTerminalSession[] {
  const matchedTmux = makeTmuxSession({ name: "matched", socket: "gobby" });
  return [
    {
      tmux: matchedTmux,
      gobby: makeGobbySession({ title: "Matched session", status: "active" }),
      label: "#1 Matched session",
      dead: false,
      agentManaged: true,
      external: false,
    },
    {
      tmux: makeTmuxSession({ name: "external", pane_dead: true }),
      gobby: null,
      label: "External shell",
      dead: true,
      agentManaged: false,
      external: true,
    },
  ];
}

describe("TerminalSessionPicker", () => {
  it("picker status dot", async () => {
    const user = userEvent.setup();
    const sessions = joinedSessions();
    render(
      <TerminalSessionPicker
        sessions={sessions}
        value={sessionKey(sessions[0].tmux)}
        onChange={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: "Terminal session" }));
    const listbox = screen.getByRole("listbox");
    const matched = within(listbox).getByRole("option", { name: /Matched session/ });
    const external = within(listbox).getByRole("option", { name: /External shell/ });

    expect(within(matched).getByRole("img", { name: "Session active" })).toBeInTheDocument();
    expect(within(external).queryByRole("img")).not.toBeInTheDocument();
  });

  it("renders badges and reports controlled selection", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const sessions = joinedSessions();
    render(
      <TerminalSessionPicker
        sessions={sessions}
        value={sessionKey(sessions[0].tmux)}
        onChange={onChange}
      />,
    );

    const trigger = screen.getByRole("combobox", { name: "Terminal session" });
    expect(trigger).toHaveTextContent("Matched session");
    await user.click(trigger);

    const listbox = screen.getByRole("listbox");
    expect(within(listbox).getByText("Agent-managed")).toBeInTheDocument();
    expect(within(listbox).getByText("Dead")).toBeInTheDocument();
    expect(within(listbox).getByText("External")).toBeInTheDocument();

    await user.click(within(listbox).getByRole("option", { name: /External shell/ }));
    expect(onChange).toHaveBeenCalledWith(sessionKey(sessions[1].tmux));
  });
});
