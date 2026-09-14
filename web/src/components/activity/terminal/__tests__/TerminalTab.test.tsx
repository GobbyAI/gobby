import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { forwardRef, useImperativeHandle, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TerminalAttachHistory } from "../../../../hooks/terminalOutputSink";
import type { GobbySession } from "../../../../types/sessions";
import type { JoinedTerminalSession } from "../terminalSessions";
import type { TerminalViewHandle, TerminalViewProps } from "../TerminalView";
import { TerminalTab } from "../TerminalTab";
import {
  makeHookState,
  makeTmuxSession,
  type HookResult,
} from "./terminalTabFixtures";

const mockUseTmuxSessions = vi.hoisted(() => vi.fn<() => HookResult>());
const terminalViewState = vi.hoisted(() => ({ mounts: 0 }));

vi.mock("../../../../hooks/useTmuxSessions", () => ({
  useTmuxSessions: mockUseTmuxSessions,
}));

vi.mock("../TerminalSessionList", () => ({
  TerminalSessionList: ({
    sessions,
    value,
    onChange,
    onTerminate,
  }: {
    sessions: JoinedTerminalSession[];
    value: string | null;
    onChange: (value: string) => void;
    onTerminate: (session: JoinedTerminalSession) => void;
  }) => (
    <>
      <select
        aria-label="Terminal session"
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value)}
      >
        {sessions.map((session) => (
          <option
            key={session.tmux.terminal_id}
            value={session.tmux.terminal_id}
            data-external={session.external}
          >
            {session.label}
          </option>
        ))}
      </select>
      {sessions.map((session) => (
        <button
          key={`terminate-${session.tmux.terminal_id}`}
          type="button"
          onClick={() => onTerminate(session)}
        >
          Terminate {session.label}
        </button>
      ))}
    </>
  ),
}));

vi.mock("../TerminalView", () => ({
  TerminalView: forwardRef<TerminalViewHandle, TerminalViewProps>(
    (props, ref) => {
      const [mountId] = useState(() => ++terminalViewState.mounts);
      const [writes, setWrites] = useState<string[]>([]);
      const [keyboard, setKeyboard] = useState("down");
      useImperativeHandle(ref, () => ({
        write: (data: string) => setWrites((current) => [...current, data]),
        getSize: () => ({ rows: 24, cols: 80 }),
        setKeyboardOpen: (open: boolean) =>
          setKeyboard(open ? "raised" : "lowered"),
        applyAttachHistory: (
          text: string,
          truncated: boolean,
          unavailable: boolean,
        ) =>
          setWrites((current) => [
            ...current,
            `history(${String(truncated)},${String(unavailable)}):${text}`,
          ]),
      }));
      return (
        <div
          role="log"
          aria-label="Terminal output (read-only)"
          data-mount-id={mountId}
          onKeyDown={() => undefined}
        >
          <output aria-label="Terminal writes">{writes.join("")}</output>
          <button type="button" onClick={() => props.onReady?.(31, 97)}>
            Renderer ready
          </button>
          <button
            type="button"
            onClick={() => props.onProtocolResponse?.("\u001b[6n")}
          >
            Protocol reply
          </button>
          <button type="button" onClick={() => props.onProtocolResponse?.("c")}>
            Typed c
          </button>
          <output aria-label="Terminal min cols">{props.minCols ?? ""}</output>
          <output aria-label="Terminal keyboard">{keyboard}</output>
          <output aria-label="Terminal keyboard on demand">
            {String(props.keyboardOnDemand ?? false)}
          </output>
          <button type="button" onClick={() => props.onBlur?.()}>
            Leave terminal
          </button>
          <button type="button" onClick={() => props.onSizeChange?.(33, 101)}>
            Renderer resized
          </button>
        </div>
      );
    },
  ),
}));

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
    handoff_markdown: null,
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

let hookState: HookResult;
let outputListener: ((runId: string, data: string) => void) | null;
let historyListener: ((history: TerminalAttachHistory) => void) | null;

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
  // Most fixtures are bare tmux panes; show every session unless a test
  // exercises the agents-only default explicitly.
  window.localStorage.setItem("gobby:terminal:session-scope", "all");
  outputListener = null;
  historyListener = null;
  terminalViewState.mounts = 0;
  hookState = makeHookState({
    onOutput: vi.fn((listener) => {
      outputListener = listener;
    }),
    onAttachHistory: vi.fn((listener) => {
      historyListener = listener;
    }),
  });
  mockUseTmuxSessions.mockReset();
  mockUseTmuxSessions.mockImplementation(() => hookState);
});

describe("attach lifecycle", () => {
  it("auto-selects the first live session and attaches with its socket identity", async () => {
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [
        makeTmuxSession({ name: "dead-shell", pane_dead: true }),
        makeTmuxSession({ name: "agent", socket: "gobby" }),
      ],
    });

    render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "gobby:agent",
        "gobby",
      );
    });
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toHaveValue("gobby:agent");
  });

  it("restores the exact selected target after a component remount", async () => {
    const user = userEvent.setup();
    // A row-id that is NOT derived from socket:name — restore must match on
    // the terminals-row identity, not the legacy socket-qualified key.
    const rowId = "22222222-3333-4444-8555-666666666666";
    const sessions = [
      makeTmuxSession({ name: "one", socket: "default" }),
      makeTmuxSession({ name: "two", socket: "gobby", terminal_id: rowId }),
    ];
    hookState = makeHookState({ sessionsLoaded: true, sessions });
    const firstRender = render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "default:one",
        "default",
      );
    });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Terminal session" }),
      rowId,
    );
    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(rowId, "gobby");
      expect(
        JSON.parse(
          window.sessionStorage.getItem("gobby:terminal:selected-target") ??
            "null",
        ),
      ).toEqual({ terminal_id: rowId });
    });

    firstRender.unmount();
    hookState = makeHookState({ sessionsLoaded: true, sessions });
    render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledTimes(1);
      expect(hookState.attachSession).toHaveBeenCalledWith(rowId, "gobby");
    });
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toHaveValue(rowId);
  });

  it("selects the first row when every pane is dead and hides the keys bar", async () => {
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "finished", pane_dead: true })],
    });

    render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "default:finished",
        "default",
      );
    });
    expect(
      screen.getByRole("log", { name: "Terminal output (read-only)" }),
    ).toBeInTheDocument();
    // A dead pane accepts no input, so the special-keys bar is withheld.
    expect(screen.queryByRole("button", { name: "Esc" })).toBeNull();
  });

  it("detaches before switching targets and filters globally broadcast output", async () => {
    const user = userEvent.setup();
    const first = makeTmuxSession({ name: "one" });
    const second = makeTmuxSession({ name: "two", socket: "gobby" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [first, second],
      attachedTarget: { terminal_id: "default:one" },
      streamingId: "run-one",
      onOutput: vi.fn((listener) => {
        outputListener = listener;
      }),
    });
    const rendered = render(<TerminalTab />);

    await waitFor(() => expect(outputListener).not.toBeNull());
    act(() => outputListener?.("another-run", "wrong"));
    expect(
      screen.getByRole("status", { name: "Terminal writes" }),
    ).toHaveTextContent("");
    act(() => outputListener?.("run-one", "right"));
    expect(
      screen.getByRole("status", { name: "Terminal writes" }),
    ).toHaveTextContent("right");

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Terminal session" }),
      "gobby:two",
    );
    await waitFor(() =>
      expect(hookState.detachSession).toHaveBeenCalledTimes(1),
    );
    expect(hookState.clearAttachError).toHaveBeenCalledTimes(1);
    expect(hookState.dismissEndedSession).not.toHaveBeenCalled();

    hookState = { ...hookState, requestPending: true };
    rendered.rerender(<TerminalTab />);
    rendered.rerender(<TerminalTab />);
    expect(hookState.detachSession).toHaveBeenCalledTimes(1);

    hookState = {
      ...hookState,
      requestPending: false,
      attachedTarget: null,
      streamingId: null,
    };
    rendered.rerender(<TerminalTab />);
    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "gobby:two",
        "gobby",
      );
    });
  });

  it("applies attach history before the first streamed output and drops stale windows", async () => {
    const tmux = makeTmuxSession({ name: "one" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { terminal_id: "default:one" },
      streamingId: "run-one",
      onOutput: vi.fn((listener) => {
        outputListener = listener;
      }),
      onAttachHistory: vi.fn((listener) => {
        historyListener = listener;
      }),
    });
    render(<TerminalTab />);

    await waitFor(() => expect(historyListener).not.toBeNull());

    // A superseded attachment must not paint into the replacement's terminal.
    act(() =>
      historyListener?.({
        streamingId: "run-stale",
        text: "stale",
        truncated: false,
        unavailable: false,
        droppedBytes: 0,
        totalBytes: 5,
      }),
    );
    expect(
      screen.getByRole("status", { name: "Terminal writes" }),
    ).toHaveTextContent("");

    act(() =>
      historyListener?.({
        streamingId: "run-one",
        text: "older",
        truncated: true,
        unavailable: false,
        droppedBytes: 12,
        totalBytes: 40,
      }),
    );
    act(() => outputListener?.("run-one", "live"));

    expect(
      screen.getByRole("status", { name: "Terminal writes" }),
    ).toHaveTextContent("history(true,false):olderlive");
  });

  it("defers focus consumption until a gobby-socket agent row can be joined", async () => {
    const onFocusHandled = vi.fn();
    const gobbySession = makeGobbySession({
      id: "focus-me",
      agent_run_id: "agent-run-7",
    });
    const tmux = makeTmuxSession({
      name: "agent-pane",
      socket: "gobby",
      agent_run_id: "agent-run-7",
      agent_managed: true,
    });
    hookState = makeHookState({ sessions: [tmux], sessionsLoaded: false });
    const rendered = render(
      <TerminalTab
        sessions={[gobbySession]}
        focusSessionId="focus-me"
        onFocusHandled={onFocusHandled}
      />,
    );

    expect(onFocusHandled).not.toHaveBeenCalled();
    expect(hookState.attachSession).not.toHaveBeenCalled();

    hookState = { ...hookState, sessionsLoaded: true };
    rendered.rerender(
      <TerminalTab
        sessions={[gobbySession]}
        focusSessionId="focus-me"
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "gobby:agent-pane",
        "gobby",
      );
    });
    expect(onFocusHandled).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toHaveValue("gobby:agent-pane");
  });

  it("gates the empty state on the first list and explains how to create a session", async () => {
    const user = userEvent.setup();
    hookState = makeHookState({ sessionsLoaded: false });
    const rendered = render(<TerminalTab />);

    expect(screen.getByText("Loading terminal sessions…")).toBeInTheDocument();
    expect(screen.queryByText("No terminal sessions")).not.toBeInTheDocument();

    hookState = { ...hookState, sessionsLoaded: true };
    rendered.rerender(<TerminalTab />);
    expect(await screen.findByText("No terminal sessions")).toBeInTheDocument();
    expect(
      screen.getByText(/Create one to start a live, read-only terminal view/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New Terminal" }));
    expect(hookState.createSession).toHaveBeenCalledTimes(1);
  });

  it("creates, selects, and attaches a new external terminal exactly once", async () => {
    const user = userEvent.setup();
    const createSession = vi.fn();
    const attachSession = vi.fn();
    hookState = makeHookState({
      sessionsLoaded: true,
      createSession,
      attachSession,
    });
    const rendered = render(<TerminalTab />);

    await user.click(screen.getByRole("button", { name: "New Terminal" }));
    expect(createSession).toHaveBeenCalledTimes(1);
    expect(createSession).toHaveBeenCalledWith();

    hookState = { ...hookState, requestPending: true };
    rendered.rerender(<TerminalTab />);
    expect(screen.getByRole("button", { name: "New Terminal" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "New Terminal" }));
    expect(createSession).toHaveBeenCalledTimes(1);

    hookState = {
      ...hookState,
      requestPending: false,
      createdSession: { terminal_id: "default:web-new" },
      sessions: [makeTmuxSession({ name: "web-new", socket: "default" })],
    };
    rendered.rerender(<TerminalTab />);

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toHaveValue("default:web-new");
      expect(attachSession).toHaveBeenCalledWith("default:web-new", "default");
    });
    expect(screen.getByRole("option", { name: "web-new" })).toHaveAttribute(
      "data-external",
      "true",
    );
  });

  it("reports a missing focus target once, then selects the normal fallback", async () => {
    const onFocusHandled = vi.fn();
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "fallback" })],
    });

    render(
      <TerminalTab focusSessionId="missing" onFocusHandled={onFocusHandled} />,
    );

    expect(
      await screen.findByText("No live terminal for this session"),
    ).toBeInTheDocument();
    expect(onFocusHandled).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "default:fallback",
        "default",
      );
    });
  });
});

describe("ready handshake repaint", () => {
  it("keeps one renderer across replacements and retires the scrim on history", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "wide" });
    const mountId = () =>
      screen
        .getByRole("log", { name: "Terminal output (read-only)" })
        .getAttribute("data-mount-id");
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      onAttachHistory: vi.fn((listener) => {
        historyListener = listener;
      }),
    });
    const rendered = render(<TerminalTab />);

    expect(await screen.findByText("Attaching terminal…")).toBeInTheDocument();
    const firstMountId = mountId();
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));
    // Nothing is attached, so there is no attachment to address a frame to.
    expect(hookState.resizeTerminal).not.toHaveBeenCalled();
    expect(hookState.refreshTerminal).not.toHaveBeenCalled();

    hookState = {
      ...hookState,
      attachedTarget: { terminal_id: "default:wide" },
      streamingId: "stream-wide",
      requestPending: false,
    };
    rendered.rerender(<TerminalTab />);
    expect(mountId()).toBe(firstMountId);
    expect(screen.getByText("Attaching terminal…")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Renderer ready" }));
    expect(hookState.resizeTerminal).toHaveBeenCalledTimes(1);
    expect(hookState.resizeTerminal).toHaveBeenLastCalledWith(31, 97);
    // The resize is the activation signal; the daemon owns the repaint that
    // follows history, so the client never issues one of its own.
    expect(hookState.refreshTerminal).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.queryByText("Attaching terminal…")).not.toBeInTheDocument();
    });

    // A reconnect installs a replacement attachment. The renderer is not
    // remounted for it, so `onReady` will not fire again and cannot be what
    // retires the scrim. The scrim covers the pane and swallows scroll, so
    // leaving it up is a real defect rather than a cosmetic one.
    hookState = { ...hookState, streamingId: "stream-replacement" };
    rendered.rerender(<TerminalTab />);
    expect(mountId()).toBe(firstMountId);
    expect(screen.getByText("Attaching terminal…")).toBeInTheDocument();

    act(() =>
      historyListener?.({
        streamingId: "stream-replacement",
        text: "restored",
        truncated: false,
        unavailable: false,
        droppedBytes: 0,
        totalBytes: 8,
      }),
    );
    await waitFor(() => {
      expect(screen.queryByText("Attaching terminal…")).not.toBeInTheDocument();
    });
    expect(mountId()).toBe(firstMountId);
    expect(hookState.refreshTerminal).not.toHaveBeenCalled();
  });

  it("requires fresh readiness when a reconnect reuses a stream id", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "reused" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { terminal_id: "default:reused" },
      streamingId: "same-stream",
    });
    const rendered = render(<TerminalTab />);

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toHaveValue("default:reused");
    });
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));

    hookState = {
      ...hookState,
      connected: false,
      sessionsLoaded: false,
      attachedTarget: null,
      streamingId: null,
    };
    rendered.rerender(<TerminalTab />);
    expect(await screen.findByText("Reconnecting")).toBeInTheDocument();

    hookState = {
      ...hookState,
      connected: true,
      sessionsLoaded: true,
      attachedTarget: { terminal_id: "default:reused" },
      streamingId: "same-stream",
    };
    rendered.rerender(<TerminalTab />);
    expect(screen.getByText("Attaching terminal…")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));
    expect(hookState.resizeTerminal).toHaveBeenCalledTimes(2);
  });
});

describe("terminate action", () => {
  it("kills the row's tmux session via the list terminate action", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "doomed", socket: "gobby" });
    hookState = makeHookState({ sessionsLoaded: true, sessions: [tmux] });
    render(<TerminalTab />);

    await user.click(
      await screen.findByRole("button", { name: "Terminate doomed" }),
    );
    expect(hookState.killSession).toHaveBeenCalledTimes(1);
    expect(hookState.killSession).toHaveBeenCalledWith("gobby:doomed");
  });
});

describe("direct input", () => {
  it("forwards renderer input and quick-keys composer input without a gate", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "interactive" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { terminal_id: "default:interactive" },
      streamingId: "stream-input",
    });
    render(<TerminalTab />);
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));

    // Renderer-emitted input — typed keys and protocol replies alike — flows
    // straight through to the PTY. There is no enable-input gate anymore.
    await user.click(screen.getByRole("button", { name: "Protocol reply" }));
    expect(hookState.sendInput).toHaveBeenCalledWith("\u001b[6n");

    await user.click(screen.getByRole("button", { name: "Esc" }));
    expect(hookState.sendInput).toHaveBeenCalledWith("\u001b");
  });
});

describe("attach error and reconnect gating", () => {
  it("suppresses attach sends until the pending request clears", async () => {
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "pending" })],
      requestPending: true,
    });
    const rendered = render(<TerminalTab />);

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toHaveValue("default:pending");
    });
    rendered.rerender(<TerminalTab />);
    expect(hookState.attachSession).not.toHaveBeenCalled();

    hookState = { ...hookState, requestPending: false };
    rendered.rerender(<TerminalTab />);
    await waitFor(() =>
      expect(hookState.attachSession).toHaveBeenCalledTimes(1),
    );
  });

  it("halts on attach error and retry re-arms exactly one request", async () => {
    const user = userEvent.setup();
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "retry-me" })],
      attachError: "Permission denied",
    });
    const rendered = render(<TerminalTab />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn’t attach to this terminal. Permission denied",
    );
    expect(hookState.attachSession).not.toHaveBeenCalled();
    await user.click(
      screen.getByRole("button", { name: "Retry terminal attach" }),
    );
    expect(hookState.clearAttachError).toHaveBeenCalledTimes(1);
    expect(hookState.attachSession).not.toHaveBeenCalled();

    hookState = { ...hookState, attachError: null };
    rendered.rerender(<TerminalTab />);
    await waitFor(() =>
      expect(hookState.attachSession).toHaveBeenCalledTimes(1),
    );
    rendered.rerender(<TerminalTab />);
    expect(hookState.attachSession).toHaveBeenCalledTimes(1);
  });

  it("uses the socket-qualified last attachment to detect a vanished reconnect target", async () => {
    const user = userEvent.setup();
    const defaultShared = makeTmuxSession({
      name: "shared",
      socket: "default",
    });
    const gobbyShared = makeTmuxSession({ name: "shared", socket: "gobby" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [defaultShared, gobbyShared],
      attachedTarget: { terminal_id: "default:shared" },
      streamingId: "stream-shared",
    });
    const rendered = render(<TerminalTab />);
    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toHaveValue("default:shared");
    });

    hookState = {
      ...hookState,
      connected: false,
      sessionsLoaded: false,
      attachedTarget: null,
      streamingId: null,
      requestPending: false,
    };
    rendered.rerender(<TerminalTab />);
    expect(await screen.findByText("Reconnecting")).toBeInTheDocument();

    hookState = {
      ...hookState,
      connected: true,
      sessionsLoaded: true,
      sessions: [gobbyShared],
    };
    rendered.rerender(<TerminalTab />);
    expect(
      await screen.findByText("Terminal session ended"),
    ).toBeInTheDocument();
    expect(hookState.attachSession).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "Dismiss ended session" }),
    );
    expect(hookState.attachSession).not.toHaveBeenCalled();
    expect(
      window.sessionStorage.getItem("gobby:terminal:selected-target"),
    ).toBeNull();

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Terminal session" }),
      "gobby:shared",
    );
    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "gobby:shared",
        "gobby",
      );
    });
  });

  it("retries once when a mid-attach target survives reconnect", async () => {
    const target = makeTmuxSession({ name: "survivor" });
    hookState = makeHookState({ sessionsLoaded: true, sessions: [target] });
    const rendered = render(<TerminalTab />);
    await waitFor(() =>
      expect(hookState.attachSession).toHaveBeenCalledTimes(1),
    );

    hookState = {
      ...hookState,
      connected: false,
      sessionsLoaded: false,
      requestPending: false,
    };
    rendered.rerender(<TerminalTab />);
    hookState = { ...hookState, connected: true, sessionsLoaded: true };
    rendered.rerender(<TerminalTab />);

    await waitFor(() =>
      expect(hookState.attachSession).toHaveBeenCalledTimes(2),
    );
  });

  it("ends when a mid-attach target is absent from the first reconnect list", async () => {
    const target = makeTmuxSession({ name: "lost" });
    hookState = makeHookState({ sessionsLoaded: true, sessions: [target] });
    const rendered = render(<TerminalTab />);
    await waitFor(() =>
      expect(hookState.attachSession).toHaveBeenCalledTimes(1),
    );

    hookState = {
      ...hookState,
      connected: false,
      sessionsLoaded: false,
      requestPending: false,
    };
    rendered.rerender(<TerminalTab />);
    hookState = {
      ...hookState,
      connected: true,
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "replacement" })],
    };
    rendered.rerender(<TerminalTab />);

    expect(
      await screen.findByText("Terminal session ended"),
    ).toBeInTheDocument();
    expect(hookState.attachSession).not.toHaveBeenCalledWith(
      "replacement",
      "default",
    );
  });
});

describe("sticky Ctrl", () => {
  it("folds a sticky Ctrl into the next typed letter and leaves other bytes armed", async () => {
    const user = userEvent.setup();
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "interactive" })],
      attachedTarget: { terminal_id: "default:interactive" },
      streamingId: "stream-input",
    });
    render(<TerminalTab />);
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));

    const ctrl = screen.getByRole("button", { name: "Ctrl" });
    await user.click(ctrl);
    // A protocol reply is not the key Ctrl was armed for.
    await user.click(screen.getByRole("button", { name: "Protocol reply" }));
    expect(hookState.sendInput).toHaveBeenLastCalledWith("\u001b[6n");
    expect(ctrl).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: "Typed c" }));
    expect(hookState.sendInput).toHaveBeenLastCalledWith("\u0003");
    expect(ctrl).toHaveAttribute("aria-pressed", "false");
  });
});

describe("session scope", () => {
  it("hides panes without a Gobby session by default and offers the full list", async () => {
    const user = userEvent.setup();
    window.localStorage.clear();
    const agentSession = makeGobbySession({ id: "agent-session" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [
        makeTmuxSession({ name: "tail" }),
        makeTmuxSession({ name: "agent", gobby_session_id: "agent-session" }),
      ],
    });
    const first = render(<TerminalTab sessions={[agentSession]} />);

    const list = screen.getByRole("combobox", { name: "Terminal session" });
    expect(
      Array.from(list.querySelectorAll("option")).map((o) => o.value),
    ).toEqual(["default:agent"]);
    await waitFor(() => expect(list).toHaveValue("default:agent"));
    first.unmount();

    // With no agent session at all the list explains itself and offers the
    // recovery; the choice persists. Drop the first mount's remembered target
    // so the second mount does not open on the "session ended" notice.
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "tail" })],
    });
    window.localStorage.clear();
    window.sessionStorage.clear();
    render(<TerminalTab />);
    await user.click(screen.getByRole("button", { name: "Show all sessions" }));
    expect(
      screen.queryByRole("button", { name: "Show all sessions" }),
    ).toBeNull();
    expect(window.localStorage.getItem("gobby:terminal:session-scope")).toBe(
      "all",
    );
  });

  it("drops the 80-column floor on the mobile tier so the PTY wraps", () => {
    const mobileMatchMedia = vi.fn((query: string) => ({
      matches: query.includes("max-width"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    vi.stubGlobal("matchMedia", mobileMatchMedia);
    try {
      hookState = makeHookState({
        sessionsLoaded: true,
        sessions: [makeTmuxSession({ name: "shell" })],
      });
      render(<TerminalTab />);
      expect(screen.getByLabelText("Terminal min cols")).toHaveTextContent("1");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("typing mode", () => {
  const ROTATE_PROMPT = "Turn your phone upright to type";

  function interactiveHookState(): HookResult {
    return makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "interactive" })],
      attachedTarget: { terminal_id: "default:interactive" },
      streamingId: "stream-input",
    });
  }

  /** A touch device; `rotate` flips phone landscape and fires the listeners. */
  function stubTouchDevice(landscape: boolean) {
    let isLandscape = landscape;
    const listeners = new Map<string, (event: MediaQueryListEvent) => void>();
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches:
          query === "(pointer: coarse)" ||
          (query.includes("orientation") && isLandscape),
        media: query,
        addEventListener: (
          _type: string,
          listener: (event: MediaQueryListEvent) => void,
        ) => listeners.set(query, listener),
        removeEventListener: vi.fn(),
      })),
    );
    return (matches: boolean) => {
      isLandscape = matches;
      act(() => {
        for (const [query, listener] of listeners) {
          if (query.includes("orientation")) {
            listener({ matches } as MediaQueryListEvent);
          }
        }
      });
    };
  }

  it("expands the terminal over the session list and collapses back", async () => {
    const user = userEvent.setup();
    const onExpandedChange = vi.fn();
    hookState = interactiveHookState();
    render(<TerminalTab onExpandedChange={onExpandedChange} />);

    // A fine pointer keeps the keyboard following focus: the keys bar shows,
    // but without a keyboard key.
    await screen.findByRole("button", { name: "Ctrl" });
    expect(screen.queryByRole("button", { name: "Keyboard" })).toBeNull();
    expect(
      screen.getByLabelText("Terminal keyboard on demand"),
    ).toHaveTextContent("false");

    await user.click(screen.getByRole("button", { name: "Expand terminal" }));
    expect(
      screen.queryByRole("combobox", { name: "Terminal session" }),
    ).toBeNull();
    expect(onExpandedChange).toHaveBeenLastCalledWith(true);

    await user.click(screen.getByRole("button", { name: "Collapse terminal" }));
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toBeInTheDocument();
    expect(onExpandedChange).toHaveBeenLastCalledWith(false);
  });

  it("raises the keyboard expanded and restores the layout when focus leaves", async () => {
    const user = userEvent.setup();
    stubTouchDevice(false);
    try {
      hookState = interactiveHookState();
      render(<TerminalTab />);
      expect(
        screen.getByLabelText("Terminal keyboard on demand"),
      ).toHaveTextContent("true");

      const key = await screen.findByRole("button", { name: "Keyboard" });
      await user.click(key);
      expect(screen.getByLabelText("Terminal keyboard")).toHaveTextContent(
        "raised",
      );
      expect(key).toHaveAttribute("aria-pressed", "true");
      expect(
        screen.queryByRole("combobox", { name: "Terminal session" }),
      ).toBeNull();

      await user.click(screen.getByRole("button", { name: "Leave terminal" }));
      expect(key).toHaveAttribute("aria-pressed", "false");
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toBeInTheDocument();
      expect(hookState.releaseControl).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("sizes the raised-keyboard layout to the visual viewport as it moves", async () => {
    const user = userEvent.setup();
    stubTouchDevice(false);
    const viewport = Object.assign(new EventTarget(), {
      height: 800,
      offsetTop: 0,
    });
    vi.stubGlobal("visualViewport", viewport);
    try {
      hookState = interactiveHookState();
      const { container } = render(<TerminalTab />);
      const root = container.firstElementChild as HTMLElement;
      const key = await screen.findByRole("button", { name: "Keyboard" });
      expect(root.style.height).toBe("");

      await user.click(key);
      expect(root.style.height).toBe("800px");

      // The keyboard slides up, then iOS scrolls the visual viewport. The
      // root follows both, and the pane's ResizeObserver refits the rows
      // from that box (covered end to end in TerminalTabViewport.test.tsx).
      act(() => {
        viewport.height = 460;
        viewport.dispatchEvent(new Event("resize"));
      });
      expect(root.style.height).toBe("460px");
      act(() => {
        viewport.offsetTop = 40;
        viewport.dispatchEvent(new Event("scroll"));
      });
      expect(root.style.height).toBe("500px");

      await user.click(key);
      expect(root.style.height).toBe("");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("asks for portrait in phone landscape and lowers a raised keyboard on rotation", async () => {
    const user = userEvent.setup();
    const rotate = stubTouchDevice(true);
    try {
      hookState = interactiveHookState();
      render(<TerminalTab />);
      const key = await screen.findByRole("button", { name: "Keyboard" });

      await user.click(key);
      expect(screen.getByText(ROTATE_PROMPT)).toBeInTheDocument();
      expect(screen.getByLabelText("Terminal keyboard")).toHaveTextContent(
        "down",
      );
      expect(key).toHaveAttribute("aria-pressed", "false");

      // A tap on the scrim or leaving the terminal dismisses the prompt.
      await user.click(screen.getByText(ROTATE_PROMPT));
      expect(screen.queryByText(ROTATE_PROMPT)).toBeNull();
      await user.click(key);
      await user.click(screen.getByRole("button", { name: "Leave terminal" }));
      expect(screen.queryByText(ROTATE_PROMPT)).toBeNull();

      await user.click(key);
      rotate(false);
      expect(screen.queryByText(ROTATE_PROMPT)).toBeNull();
      await user.click(key);
      expect(screen.getByLabelText("Terminal keyboard")).toHaveTextContent(
        "raised",
      );

      rotate(true);
      expect(screen.getByLabelText("Terminal keyboard")).toHaveTextContent(
        "lowered",
      );
      expect(screen.getByText(ROTATE_PROMPT)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Expand terminal" }),
      ).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
