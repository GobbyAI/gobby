import { vi } from "vitest";

import type { TmuxSession } from "../../../../hooks/terminalRosterSnapshot";
import { EMPTY_WRITE_SETTLEMENT } from "../../../../hooks/terminalWriteSettlement";
import type { useTmuxSessions } from "../../../../hooks/useTmuxSessions";

export type HookResult = ReturnType<typeof useTmuxSessions>;

export function makeTmuxSession(
  overrides: Partial<TmuxSession> = {},
): TmuxSession {
  const name = overrides.name ?? "shell";
  const socket = overrides.socket ?? "default";
  return {
    terminal_id: overrides.terminal_id ?? `${socket}:${name}`,
    backend: "tmux",
    ownership: socket === "gobby" ? "gobby" : "external",
    state: "live",
    title: name,
    session_id: overrides.session_id ?? overrides.gobby_session_id ?? null,
    agent_run_id: overrides.agent_run_id ?? null,
    dims: null,
    name,
    socket,
    pane_pid: 123,
    pane_dead: false,
    pane_title: null,
    pane_command: null,
    pane_path: null,
    window_name: null,
    session_title: null,
    gobby_session_id: null,
    agent_managed: false,
    attached_bridge: null,
    ...overrides,
  };
}

export function makeHookState(overrides: Partial<HookResult> = {}): HookResult {
  return {
    sessions: [],
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
    reportViewport: vi.fn(),
    createSession: vi.fn(),
    killSession: vi.fn(),
    refreshSessions: vi.fn(),
    dismissEndedSession: vi.fn(),
    hasControl: false,
    controlPending: false,
    leaseLost: false,
    pendingWrite: null,
    writeRefusal: null,
    writeSettlement: EMPTY_WRITE_SETTLEMENT,
    takeControl: vi.fn(),
    releaseControl: vi.fn(),
    sendInput: vi.fn(),
    sendPaste: vi.fn(),
    retryWrite: vi.fn(),
    discardWrite: vi.fn(),
    dismissWriteRefusal: vi.fn(),
    resizeTerminal: vi.fn(),
    onOutput: vi.fn(),
    onAttachHistory: vi.fn(),
    ...overrides,
  };
}
