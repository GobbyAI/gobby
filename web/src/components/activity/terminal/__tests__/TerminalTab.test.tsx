import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { forwardRef, useImperativeHandle, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  TerminalAttachHistory,
  TmuxSession,
  useTmuxSessions,
} from "../../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../../types/sessions";
import type { JoinedTerminalSession } from "../terminalSessions";
import type { TerminalViewHandle, TerminalViewProps } from "../TerminalView";
import { TerminalTab } from "../TerminalTab";

type HookResult = ReturnType<typeof useTmuxSessions>;

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
            key={`${session.tmux.socket}:${session.tmux.name}`}
            value={`${session.tmux.socket}:${session.tmux.name}`}
            data-external={session.external}
          >
            {session.label}
          </option>
        ))}
      </select>
      {sessions.map((session) => (
        <button
          key={`terminate-${session.tmux.socket}:${session.tmux.name}`}
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
      useImperativeHandle(ref, () => ({
        write: (data: string) => setWrites((current) => [...current, data]),
        getSize: () => ({ rows: 24, cols: 80 }),
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
          <button type="button" onClick={() => props.onSizeChange?.(33, 101)}>
            Renderer resized
          </button>
        </div>
      );
    },
  ),
}));

function makeTmuxSession(overrides: Partial<TmuxSession> = {}): TmuxSession {
  return {
    name: "shell",
    socket: "default",
    pane_pid: 123,
    pane_dead: false,
    pane_title: null,
    pane_command: null,
    pane_path: null,
    window_name: null,
    session_title: null,
    gobby_session_id: null,
    agent_managed: false,
    agent_run_id: null,
    attached_bridge: null,
    ...overrides,
  };
}

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

function makeHookState(overrides: Partial<HookResult> = {}): HookResult {
  return {
    sessions: [],
    liveCliSessionIds: [],
    connected: true,
    sessionsLoaded: false,
    attachedTarget: null,
    streamingId: null,
    isLoading: false,
    sessionEnded: false,
    requestPending: false,
    attachError: null,
    createdSession: null,
    attachSession: vi.fn(),
    detachSession: vi.fn(),
    clearAttachError: vi.fn(),
    refreshTerminal: vi.fn(),
    createSession: vi.fn(),
    killSession: vi.fn(),
    refreshSessions: vi.fn(),
    dismissEndedSession: vi.fn(),
    sendInput: vi.fn(),
    resizeTerminal: vi.fn(),
    onOutput: vi.fn(),
    onAttachHistory: vi.fn(),
    ...overrides,
  };
}

let hookState: HookResult;
let outputListener: ((runId: string, data: string) => void) | null;
let historyListener: ((history: TerminalAttachHistory) => void) | null;

beforeEach(() => {
  window.sessionStorage.clear();
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
      expect(hookState.attachSession).toHaveBeenCalledWith("agent", "gobby");
    });
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toHaveValue("gobby:agent");
  });

  it("restores the exact selected target after a component remount", async () => {
    const user = userEvent.setup();
    const sessions = [
      makeTmuxSession({ name: "one", socket: "default" }),
      makeTmuxSession({ name: "two", socket: "gobby" }),
    ];
    hookState = makeHookState({ sessionsLoaded: true, sessions });
    const firstRender = render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith("one", "default");
    });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Terminal session" }),
      "gobby:two",
    );
    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith("two", "gobby");
      expect(
        JSON.parse(
          window.sessionStorage.getItem("gobby:terminal:selected-target") ??
            "null",
        ),
      ).toEqual({ socket: "gobby", sessionName: "two" });
    });

    firstRender.unmount();
    hookState = makeHookState({ sessionsLoaded: true, sessions });
    render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledTimes(1);
      expect(hookState.attachSession).toHaveBeenCalledWith("two", "gobby");
    });
    expect(
      screen.getByRole("combobox", { name: "Terminal session" }),
    ).toHaveValue("gobby:two");
  });

  it("selects the first row when every pane is dead and hides the keys bar", async () => {
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [makeTmuxSession({ name: "finished", pane_dead: true })],
    });

    render(<TerminalTab />);

    await waitFor(() => {
      expect(hookState.attachSession).toHaveBeenCalledWith(
        "finished",
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
      attachedTarget: { name: "one", socket: "default" },
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
      expect(hookState.attachSession).toHaveBeenCalledWith("two", "gobby");
    });
  });

  it("applies attach history before the first streamed output and drops stale windows", async () => {
    const tmux = makeTmuxSession({ name: "one" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { name: "one", socket: "default" },
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
        "agent-pane",
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
      createdSession: { session_name: "web-new", socket: "default" },
      sessions: [makeTmuxSession({ name: "web-new", socket: "default" })],
    };
    rendered.rerender(<TerminalTab />);

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: "Terminal session" }),
      ).toHaveValue("default:web-new");
      expect(attachSession).toHaveBeenCalledWith("web-new", "default");
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
        "fallback",
        "default",
      );
    });
  });
});

describe("ready handshake repaint", () => {
  it("keeps attaching until view readiness and repaints each keyed replacement", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "wide" });
    hookState = makeHookState({ sessionsLoaded: true, sessions: [tmux] });
    const rendered = render(<TerminalTab />);

    expect(await screen.findByText("Attaching terminal…")).toBeInTheDocument();
    const pendingMountId = screen
      .getByRole("log", { name: "Terminal output (read-only)" })
      .getAttribute("data-mount-id");
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));
    expect(hookState.resizeTerminal).not.toHaveBeenCalled();
    expect(hookState.refreshTerminal).not.toHaveBeenCalled();

    hookState = {
      ...hookState,
      attachedTarget: { name: "wide", socket: "default" },
      streamingId: "stream-wide",
      requestPending: false,
    };
    rendered.rerender(<TerminalTab />);
    const attachedMountId = screen
      .getByRole("log", { name: "Terminal output (read-only)" })
      .getAttribute("data-mount-id");
    expect(attachedMountId).not.toBe(pendingMountId);
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

    hookState = {
      ...hookState,
      streamingId: "stream-replacement",
    };
    rendered.rerender(<TerminalTab />);
    const replacementMountId = screen
      .getByRole("log", { name: "Terminal output (read-only)" })
      .getAttribute("data-mount-id");
    expect(replacementMountId).not.toBe(attachedMountId);
    expect(screen.getByText("Attaching terminal…")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Renderer ready" }));
    expect(hookState.resizeTerminal).toHaveBeenCalledTimes(2);
    expect(hookState.resizeTerminal).toHaveBeenLastCalledWith(31, 97);
    expect(hookState.refreshTerminal).not.toHaveBeenCalled();
  });

  it("requires fresh readiness when a reconnect reuses a stream id", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "reused" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { name: "reused", socket: "default" },
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
      attachedTarget: { name: "reused", socket: "default" },
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
    expect(hookState.killSession).toHaveBeenCalledWith("doomed", "gobby");
  });
});

describe("direct input", () => {
  it("forwards renderer input and quick-keys composer input without a gate", async () => {
    const user = userEvent.setup();
    const tmux = makeTmuxSession({ name: "interactive" });
    hookState = makeHookState({
      sessionsLoaded: true,
      sessions: [tmux],
      attachedTarget: { name: "interactive", socket: "default" },
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
      attachedTarget: { name: "shared", socket: "default" },
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
      expect(hookState.attachSession).toHaveBeenCalledWith("shared", "gobby");
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
