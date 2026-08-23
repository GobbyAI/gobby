import type * as React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  act,
  fireEvent,
  render as baseRender,
  screen,
  waitFor,
} from "@testing-library/react";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import { SessionsTab } from "../SessionsTab";
import { defaultSessionsFilters } from "../sessionsFilters";
import { useActivityPanel } from "../useActivityPanel";
import { TerminalTab } from "../terminal/TerminalTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import type { GobbySession } from "../../../types/sessions";
import type { SessionMessage } from "../../../hooks/useSessionDetail";
import type {
  TerminalViewHandle,
  TerminalViewProps,
} from "../terminal/TerminalView";

type SessionDetailMock = {
  session: GobbySession | null;
  sessionError: string | null;
  transcriptDownloadUrl?: string | null;
  clearSessionError: () => void;
  messages: SessionMessage[];
  isLoading: boolean;
  transcriptStatus: { content_state: string } | null;
  hasMore?: boolean;
  loadMore?: () => void;
  hasNewer?: boolean;
  loadNewer?: () => void;
  isLoadingOlder?: boolean;
  isLoadingNewer?: boolean;
  setTranscriptAtBottom?: (atBottom: boolean) => void;
  firstItemIndex?: number;
  transcriptDegradedReason?: string | null;
};

// The tab's toolbar (selector / Filter / Search) renders in the shared panel
// header in the real layout; mount it alongside the tab so those controls are
// reachable in tests.
function HeaderHarness({ children }: { children: React.ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: React.ReactElement) =>
  baseRender(ui, { wrapper: HeaderHarness });

// The search bar is hidden until the header Search toggle opens it.
function openSearch() {
  fireEvent.click(screen.getByRole("button", { name: "Search sessions" }));
}

const mockUseSessionDetail = vi.fn<
  (sessionId?: string | null) => SessionDetailMock
>(() => ({
  session: null,
  sessionError: null,
  clearSessionError: vi.fn(),
  messages: [],
  isLoading: false,
  transcriptStatus: null,
  hasMore: false,
  loadMore: vi.fn(),
  hasNewer: false,
  loadNewer: vi.fn(),
  isLoadingOlder: false,
  isLoadingNewer: false,
  setTranscriptAtBottom: vi.fn(),
  firstItemIndex: 1_000_000,
  transcriptDegradedReason: null,
}));

const terminalHook = {
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
};

vi.mock("../../../hooks/useTmuxSessions", () => ({
  useTmuxSessions: () => ({
    sessions: [
      {
        name: "paused-pane",
        socket: "default",
        pane_pid: 4202,
        pane_dead: false,
        pane_title: "Paused Terminal",
        window_name: "agent",
        session_title: "Paused Terminal",
        gobby_session_id: "paused-1",
        agent_managed: false,
        agent_run_id: null,
        attached_bridge: null,
      },
    ],
    liveCliSessionIds: [],
    connected: true,
    sessionsLoaded: true,
    attachedTarget: null,
    streamingId: null,
    isLoading: false,
    sessionEnded: false,
    requestPending: false,
    attachError: null,
    createdSession: null,
    ...terminalHook,
  }),
}));

vi.mock("../terminal/TerminalView", async () => {
  const ReactModule = await import("react");
  return {
    TerminalView: ReactModule.forwardRef<TerminalViewHandle, TerminalViewProps>(
      function MockTerminalView(_props, ref) {
        ReactModule.useImperativeHandle(ref, () => ({
          write: vi.fn(),
          getSize: () => ({ rows: 24, cols: 80 }),
          applyAttachHistory: vi.fn(),
        }));
        return <div role="log" aria-label="Terminal output (read-only)" />;
      },
    ),
  };
});

// Virtualized lists don't render items under jsdom's zero-height viewport, so
// stand in a passthrough that renders every item (mirrors MessageList's test).
vi.mock("react-virtuoso", async () => {
  const ReactModule = await import("react");
  return {
    Virtuoso: ReactModule.forwardRef(function MockVirtuoso(
      {
        className,
        data,
        itemContent,
        computeItemKey,
        components,
        startReached,
        endReached,
      }: {
        className?: string;
        data?: unknown[];
        itemContent: (index: number, item: unknown) => React.ReactNode;
        computeItemKey?: (index: number, item: unknown) => string;
        components?: {
          Header?: React.ComponentType;
          Scroller?: React.ComponentType<{
            children?: React.ReactNode;
            className?: string;
            style?: React.CSSProperties;
          }>;
        };
        startReached?: () => void;
        endReached?: () => void;
      },
      _ref: React.ForwardedRef<unknown>,
    ) {
      const Scroller = components?.Scroller ?? "div";
      const Header = components?.Header;
      const items = data ?? [];
      return (
        <Scroller className={className} style={{ overflowY: "auto" }}>
          {Header ? <Header /> : null}
          <button
            type="button"
            data-testid="virtuoso-start-reached"
            aria-label="start reached"
            onClick={() => startReached?.()}
          />
          <button
            type="button"
            data-testid="virtuoso-end-reached"
            aria-label="end reached"
            onClick={() => endReached?.()}
          />
          {items.map((item, index) => (
            <div key={computeItemKey ? computeItemKey(index, item) : index}>
              {itemContent(index, item)}
            </div>
          ))}
        </Scroller>
      );
    }),
  };
});

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../shared/SourceIcon", () => ({
  SourceIcon: ({ source }: { source: string }) => (
    <span data-testid="source-icon">{source}</span>
  ),
}));

vi.mock("../../../hooks/useSessionDetail", () => ({
  useSessionDetail: (sessionId?: string | null) =>
    mockUseSessionDetail(sessionId),
}));

vi.mock("../../chat/MessageItem", () => ({
  MessageItem: ({ message }: { message: { content: string } }) => (
    <div data-testid="message-item">{message.content}</div>
  ),
}));

vi.mock("../../shared/MemoizedMarkdown", () => ({
  MemoizedMarkdown: ({ content }: { content: string }) => (
    <div data-testid="summary-markdown">{content}</div>
  ),
}));

vi.mock("../SessionInteractionModal", () => ({
  SessionInteractionModal: () => null,
}));

let mockFetch: MockFetchInstance;

function mockAttentionRoster() {
  mockFetch.mockJsonResponse("/api/attention/roster", {
    epoch: "test",
    seq: 0,
    entries: [],
  });
}

function mockProviderRegistry() {
  mockFetch.mockJsonResponse("/api/providers", {
    providers: [
      { name: "claude", available: true },
      { name: "codex", available: true },
    ],
  });
}

function makeSession(overrides: Partial<GobbySession>): GobbySession {
  return {
    id: "session-1",
    ref: "#201",
    external_id: "ext-201",
    source: "claude",
    project_id: "proj-1",
    title: "Terminal Session",
    status: "active",
    model: "sonnet",
    message_count: 3,
    created_at: "2026-04-08T12:00:00Z",
    updated_at: "2026-04-08T12:05:00Z",
    seq_num: 201,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { tmux_pane: "%44" },
    sandbox_enabled: false,
    sandbox_policy_hash: null,
    ...overrides,
  };
}

function getSessionEntry(label: string): HTMLElement {
  const row = screen
    .getAllByText(label)
    .map((node) => node.closest(".session-entry"))
    .find((candidate): candidate is HTMLElement => candidate != null);
  if (!row) throw new Error(`No session row found for ${label}`);
  return row;
}

const LIVE_SESSION = makeSession({
  id: "live-1",
  ref: "#201",
  external_id: "live-ext-1",
  title: "Live Terminal",
  status: "active",
  seq_num: 201,
  updated_at: "2026-04-08T12:05:00Z",
});

const PAUSED_SESSION = makeSession({
  id: "paused-1",
  ref: "#202",
  external_id: "paused-ext-1",
  title: "Paused Terminal",
  status: "paused",
  seq_num: 202,
  updated_at: "2026-04-08T12:10:00Z",
  summary_markdown: "# Session Summary",
});

function TerminalFocusHarness() {
  const activity = useActivityPanel(false);

  return (
    <>
      <SessionsTab sessions={[PAUSED_SESSION]} />
      <output aria-label="Active activity tab">{activity.activeTab}</output>
      <output aria-label="Terminal focus request">
        {activity.terminalSessionRequest ?? ""}
      </output>
      {activity.activeTab === "terminal" ? (
        <TerminalTab
          sessions={[PAUSED_SESSION]}
          focusSessionId={activity.terminalSessionRequest}
          onFocusHandled={activity.clearTerminalSessionRequest}
        />
      ) : null}
    </>
  );
}

const EXPIRED_SESSION = makeSession({
  id: "expired-1",
  ref: "#203",
  external_id: "expired-ext-1",
  title: "Expired Terminal",
  status: "expired",
  seq_num: 203,
  updated_at: "2026-04-08T12:15:00Z",
  summary_markdown: "# Expired Summary",
  terminal_context: null,
});

const MAIN_WEB_CHAT_SESSION = makeSession({
  id: "main-web-1",
  ref: "#210",
  external_id: "main-web-ext-1",
  source: "codex",
  title: "Main Web Chat",
  status: "active",
  seq_num: 210,
  session_type: "web_chat",
  terminal_context: null,
});

const PARKED_WEB_CHAT_SESSION = makeSession({
  id: "parked-web-1",
  ref: "#211",
  external_id: "parked-web-ext-1",
  source: "codex",
  title: "Parked Web Chat",
  status: "active",
  seq_num: 211,
  session_type: "web_chat",
  terminal_context: null,
});

const HANDOFF_READY_SESSION = makeSession({
  id: "handoff-1",
  ref: "#205",
  external_id: "handoff-ext-1",
  title: "Handoff Terminal",
  status: "handoff_ready",
  seq_num: 205,
  updated_at: "2026-04-08T12:18:00Z",
});

const RUNNING_AGENT_SESSION = makeSession({
  id: "agent-running-1",
  ref: "#206",
  external_id: "agent-running-ext-1",
  title: "Running Agent Terminal",
  status: "active",
  seq_num: 206,
  agent_run_id: "run-running-1",
  updated_at: "2026-04-08T12:19:00Z",
  terminal_context: { tmux_pane: "%46" },
});

const PENDING_AGENT_SESSION = makeSession({
  id: "agent-pending-1",
  ref: "#207",
  external_id: "agent-pending-ext-1",
  title: "Pending Agent Terminal",
  status: "paused",
  seq_num: 207,
  agent_run_id: "run-pending-1",
  updated_at: "2026-04-08T12:20:00Z",
  terminal_context: { tmux_pane: "%47" },
});

const COMPLETED_AGENT_SESSION = makeSession({
  id: "agent-success-1",
  ref: "#208",
  external_id: "agent-success-ext-1",
  title: "Completed Agent Terminal",
  status: "success",
  seq_num: 208,
  agent_run_id: "run-success-1",
  updated_at: "2026-04-08T12:21:00Z",
  terminal_context: { tmux_pane: "%48" },
});

const ERRORED_AGENT_SESSION = makeSession({
  id: "agent-error-1",
  ref: "#209",
  external_id: "agent-error-ext-1",
  title: "Errored Agent Terminal",
  status: "error",
  seq_num: 209,
  agent_run_id: "run-error-1",
  updated_at: "2026-04-08T12:22:00Z",
  terminal_context: { tmux_pane: "%49" },
});

const CANCELLED_AGENT_SESSION = makeSession({
  id: "agent-cancelled-1",
  ref: "#210",
  external_id: "agent-cancelled-ext-1",
  title: "Cancelled Agent Terminal",
  status: "cancelled",
  seq_num: 210,
  agent_run_id: "run-cancelled-1",
  updated_at: "2026-04-08T12:23:00Z",
  terminal_context: { tmux_pane: "%50" },
});

const PIPELINE_SESSION = makeSession({
  id: "pipeline-1",
  ref: "#204",
  external_id: "pipeline-ext-1",
  title: "Pipeline Session",
  source: "pipeline",
  status: "active",
  seq_num: 204,
  updated_at: "2026-04-08T12:20:00Z",
});

describe("SessionsTab", () => {
  beforeEach(() => {
    // SessionsTab persists the watched session id in localStorage. Without
    // clearing, a prior test's selection leaks into the next one and trips
    // selectedEntry-gated UI (Summary/Resume/Swap buttons render against null).
    localStorage.clear();
    mockUseSessionDetail.mockReset();
    terminalHook.attachSession.mockClear();
    terminalHook.detachSession.mockClear();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: "Transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
      hasNewer: false,
      loadNewer: vi.fn(),
      isLoadingNewer: false,
      setTranscriptAtBottom: vi.fn(),
    });
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockAttentionRoster();
    mockProviderRegistry();
  });

  afterEach(() => {
    vi.useRealTimers();
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("names the scoped project in the unfiltered empty state", async () => {
    render(<SessionsTab sessions={[]} projectName="Personal" />);
    expect(
      await screen.findByText("No live sessions in Personal"),
    ).toBeInTheDocument();
  });

  it("drops the scope suffix when no project name is provided", async () => {
    render(<SessionsTab sessions={[]} />);
    expect(await screen.findByText("No live sessions")).toBeInTheDocument();
  });

  it("keeps registry providers on empty filtered pages and prunes stale selections", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockAttentionRoster();
    mockFetch.mockJsonResponse("/api/providers", {
      providers: [
        { name: "qwen", available: false },
        { name: "codex", available: true },
        { name: "agy", available: true },
        { name: "cron", available: true },
        { name: "pipeline", available: true },
        { name: "system", available: true },
      ],
    });
    const filters = defaultSessionsFilters();
    filters.providers = new Set(["codex", "agy", "removed-provider"]);
    const onFiltersChange = vi.fn();
    const { rerender } = render(
      <SessionsTab
        sessions={[]}
        filters={filters}
        onFiltersChange={onFiltersChange}
      />,
    );

    await waitFor(() => {
      expect(onFiltersChange).toHaveBeenCalled();
    });
    const lastFilterChange =
      onFiltersChange.mock.calls[onFiltersChange.mock.calls.length - 1][0];
    expect(Array.from(lastFilterChange.providers)).toEqual(["codex"]);

    fireEvent.click(screen.getByRole("button", { name: "Filter sessions" }));
    expect(screen.getByLabelText("Codex")).toBeInTheDocument();
    expect(screen.getByLabelText("Qwen")).toBeInTheDocument();
    expect(screen.queryByLabelText("Cron")).toBeNull();
    expect(screen.queryByLabelText("Pipeline")).toBeNull();
    expect(screen.queryByLabelText("System")).toBeNull();
    // AGY is hidden throughout the UI and never offered as a filter (#20049).
    expect(screen.queryByLabelText("AGY")).toBeNull();

    rerender(
      <SessionsTab
        sessions={[]}
        filters={filters}
        onFiltersChange={onFiltersChange}
      />,
    );
    expect(screen.getByLabelText("Codex")).toBeInTheDocument();
    expect(screen.getByLabelText("Qwen")).toBeInTheDocument();
  });

  it("preserves running agents and surfaces polling errors until recovery", async () => {
    vi.useFakeTimers();
    mockFetch.resetRoutes();
    mockAttentionRoster();
    mockProviderRegistry();
    mockFetch.mockJsonResponse("/api/agents/running", {
      agents: [
        {
          run_id: "run-running-1",
          provider: "codex",
          session_id: "agent-running-1",
        },
      ],
    });

    render(<SessionsTab sessions={[RUNNING_AGENT_SESSION]} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(getSessionEntry("#206: Running Agent Terminal")).toBeInTheDocument();

    mockFetch.resetRoutes();
    mockAttentionRoster();
    mockProviderRegistry();
    mockFetch.mockErrorResponse("/api/agents/running", 500);
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Failed to load running agents",
    );
    expect(getSessionEntry("#206: Running Agent Terminal")).toBeInTheDocument();
    expect(consoleError).toHaveBeenCalledWith(
      "Failed to fetch running agents:",
      expect.any(Error),
    );

    mockFetch.resetRoutes();
    mockAttentionRoster();
    mockProviderRegistry();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("rejects a non-array running-agents payload without replacing prior agents", async () => {
    vi.useFakeTimers();
    mockFetch.resetRoutes();
    mockAttentionRoster();
    mockProviderRegistry();
    mockFetch.mockJsonResponse("/api/agents/running", {
      agents: [
        {
          run_id: "run-running-1",
          provider: "codex",
          session_id: "agent-running-1",
        },
      ],
    });

    render(<SessionsTab sessions={[RUNNING_AGENT_SESSION]} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    mockFetch.resetRoutes();
    mockAttentionRoster();
    mockProviderRegistry();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: {} });
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Failed to load running agents",
    );
    expect(getSessionEntry("#206: Running Agent Terminal")).toBeInTheDocument();
    expect(consoleError).toHaveBeenCalledWith(
      "Failed to fetch running agents:",
      expect.any(Error),
    );
    vi.useRealTimers();
  });

  it("auto-selects a detail row without persisting a watched session", async () => {
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session: sessionId === "live-1" ? LIVE_SESSION : PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    render(<SessionsTab sessions={[LIVE_SESSION, PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for paused-1"),
      ).toBeInTheDocument();
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBeNull();
  });

  it("persists a watched session only after explicit row selection", async () => {
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session: sessionId === "live-1" ? LIVE_SESSION : PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    render(<SessionsTab sessions={[LIVE_SESSION, PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for paused-1"),
      ).toBeInTheDocument();
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBeNull();

    fireEvent.click(getSessionEntry("#201: Live Terminal"));
    expect(localStorage.getItem("gobby-watching-session-id")).toBe("live-1");

    fireEvent.keyDown(getSessionEntry("#202: Paused Terminal"), {
      key: "Enter",
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBe("paused-1");
  });

  it("keeps the watched session selected across a transient search result", async () => {
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session: sessionId === "live-1" ? LIVE_SESSION : PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    render(<SessionsTab sessions={[LIVE_SESSION, PAUSED_SESSION]} />);

    fireEvent.click(getSessionEntry("#201: Live Terminal"));
    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for live-1"),
      ).toBeInTheDocument();
    });

    openSearch();
    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "paused-ext-1" },
    });
    await waitFor(() => {
      expect(screen.queryByText("#201: Live Terminal")).toBeNull();
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBe("live-1");

    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "" },
    });
    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for live-1"),
      ).toBeInTheDocument();
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBe("live-1");
  });

  it("restores a valid persisted watched session", async () => {
    localStorage.setItem("gobby-watching-session-id", "live-1");
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session: sessionId === "live-1" ? LIVE_SESSION : PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    render(<SessionsTab sessions={[LIVE_SESSION, PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for live-1"),
      ).toBeInTheDocument();
    });
    expect(localStorage.getItem("gobby-watching-session-id")).toBe("live-1");
  });

  it("filters live vs expired, excludes handoff and pipeline sources, and searches", async () => {
    render(
      <SessionsTab
        sessions={[
          LIVE_SESSION,
          PAUSED_SESSION,
          EXPIRED_SESSION,
          HANDOFF_READY_SESSION,
          PIPELINE_SESSION,
        ]}
        focusSessionId="live-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
      expect(screen.getByText("#201: Live Terminal")).toBeInTheDocument();
    });

    expect(screen.queryByText("#203: Expired Terminal")).toBeNull();
    expect(screen.queryByText("#205: Handoff Terminal")).toBeNull();
    expect(screen.queryByText("#204: Pipeline Session")).toBeNull();

    openSearch();
    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "paused-ext-1" },
    });
    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
      expect(screen.queryByText("#201: Live Terminal")).toBeNull();
    });

    fireEvent.change(screen.getByPlaceholderText("Search"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("radio", { name: "Expired" }));

    await waitFor(() => {
      expect(screen.getByText("#203: Expired Terminal")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: "Session actions" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("#202: Paused Terminal")).toBeNull();
    expect(screen.queryByText("#205: Handoff Terminal")).toBeNull();
  });

  it("renders provisional titles with one session ref", async () => {
    render(
      <SessionsTab
        sessions={[
          makeSession({
            id: "provisional-title",
            ref: "#9829",
            seq_num: 9829,
            title: "#9829 Codex",
          }),
        ]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#9829: Codex")).toBeInTheDocument();
    });
    expect(screen.queryByText("#9829: #9829 Codex")).toBeNull();
  });

  it("orders session entries by ref (#N) descending, not by recency", async () => {
    // Higher seq (#310) is the OLDER session; lower seq (#305) is newer. Sorting
    // by ref must still place #310 first, proving ref order beats recency.
    const olderHigherSeq = makeSession({
      id: "older-higher",
      ref: "#310",
      title: "Older Higher Seq",
      seq_num: 310,
      updated_at: "2026-04-08T12:00:00Z",
    });
    const newerLowerSeq = makeSession({
      id: "newer-lower",
      ref: "#305",
      title: "Newer Lower Seq",
      seq_num: 305,
      updated_at: "2026-04-08T13:00:00Z",
    });

    render(
      <SessionsTab
        sessions={[newerLowerSeq, olderHigherSeq]}
        focusSessionId="no-such-session"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#310: Older Higher Seq")).toBeInTheDocument();
    });

    const seqs = Array.from(document.querySelectorAll(".session-entry"))
      .map((row) => row.textContent?.match(/#(\d+):/)?.[1])
      .filter((seq): seq is string => Boolean(seq));
    expect(seqs).toEqual(["310", "305"]);
  });

  it("does not show the web chat currently rendered in the main chat", async () => {
    render(
      <SessionsTab
        sessions={[MAIN_WEB_CHAT_SESSION, PARKED_WEB_CHAT_SESSION]}
        chatSessionId="main-web-1"
        focusSessionId="parked-web-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#211: Parked Web Chat")).toBeInTheDocument();
    });

    expect(screen.queryByText("#210: Main Web Chat")).toBeNull();
  });

  it("returns a parked main web chat to the list after the main chat is cleared", async () => {
    const onFocusHandled = vi.fn();
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session:
        sessionId === "main-web-1"
          ? MAIN_WEB_CHAT_SESSION
          : PARKED_WEB_CHAT_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    const { rerender } = render(
      <SessionsTab
        sessions={[MAIN_WEB_CHAT_SESSION, PARKED_WEB_CHAT_SESSION]}
        chatSessionId="main-web-1"
        focusSessionId="parked-web-1"
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getAllByText("#211: Parked Web Chat").length,
      ).toBeGreaterThan(0);
      expect(
        screen.getByText("Transcript output for parked-web-1"),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("#210: Main Web Chat")).toBeNull();

    rerender(
      <SessionsTab
        sessions={[MAIN_WEB_CHAT_SESSION, PARKED_WEB_CHAT_SESSION]}
        chatSessionId={null}
        focusSessionId="main-web-1"
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByText("#210: Main Web Chat").length).toBeGreaterThan(
        0,
      );
      expect(
        screen.getByText("Transcript output for main-web-1"),
      ).toBeInTheDocument();
    });
    expect(screen.getAllByText("#211: Parked Web Chat").length).toBeGreaterThan(
      0,
    );
  });

  it("renders the Live | Expired status filter as a SegmentedControl", async () => {
    render(<SessionsTab sessions={[LIVE_SESSION]} focusSessionId="live-1" />);

    await waitFor(() => {
      expect(screen.getByText("#201: Live Terminal")).toBeInTheDocument();
    });

    const liveRadio = screen.getByRole("radio", { name: "Live" });
    const expiredRadio = screen.getByRole("radio", { name: "Expired" });
    expect(liveRadio).toHaveAttribute("aria-checked", "true");
    expect(expiredRadio).toHaveAttribute("aria-checked", "false");
    // Active uses the subdued tint, not the saturated bg-accent fill —
    // the SegmentedControl primitive deliberately does not expose the
    // saturated variant. See impeccable §absolute_bans / Pass 6.
    expect(liveRadio).toHaveClass("bg-accent/15");
  });

  it("renders active sessions with play status and paused sessions with pause status", async () => {
    render(
      <SessionsTab
        sessions={[LIVE_SESSION, PAUSED_SESSION]}
        focusSessionId="live-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByText("#201: Live Terminal").length).toBeGreaterThan(
        0,
      );
      expect(
        screen.getAllByText("#202: Paused Terminal").length,
      ).toBeGreaterThan(0);
    });

    const statusDotForTitle = (title: string) => {
      const row = screen
        .getAllByText(title)
        .map((node) => node.closest(".session-entry"))
        .find((candidate): candidate is HTMLElement => candidate != null);
      if (!row) throw new Error(`No session row found for ${title}`);
      const dot = row.querySelector("span.activity-row-status-dot");
      if (!dot) throw new Error(`No status dot found for ${title}`);
      return dot as HTMLSpanElement;
    };

    const activeDot = statusDotForTitle("#201: Live Terminal");
    const activeSvg = activeDot.querySelector("svg");
    expect(activeDot.getAttribute("data-kind")).toBe("active");
    expect(activeDot.getAttribute("class")).toContain(
      "animate-[pulse_1.5s_ease-in-out_infinite]",
    );
    expect(activeSvg?.getAttribute("class")).toContain(
      "activity-row-status-dot__glyph--active",
    );
    expect(activeSvg?.querySelector("polygon")).not.toBeNull();

    const pausedDot = statusDotForTitle("#202: Paused Terminal");
    const pausedSvg = pausedDot.querySelector("svg");
    expect(pausedDot.getAttribute("data-kind")).toBe("paused");
    expect(pausedDot.getAttribute("class")).not.toContain(
      "activity-row-status-dot--pulse",
    );
    expect(pausedSvg?.getAttribute("class")).toContain(
      "activity-row-status-dot__glyph--paused",
    );
    expect(pausedSvg?.querySelectorAll("rect")).toHaveLength(2);
  });

  it("renders digest fallback for a live session with no summary", async () => {
    mockUseSessionDetail.mockReturnValue({
      session: {
        ...LIVE_SESSION,
        summary_markdown: null,
        digest_markdown: "## Live digest fallback",
      },
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-live",
          role: "assistant",
          content: "Live transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    });

    render(<SessionsTab sessions={[LIVE_SESSION]} focusSessionId="live-1" />);

    await waitFor(() => {
      expect(screen.getByText("Live transcript output")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Summary" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "## Live digest fallback",
    );
  });

  it("shows Summary when digest exists in catalog metadata before detail refresh", async () => {
    const catalogSession = makeSession({
      ...LIVE_SESSION,
      digest_markdown: "## Catalog digest fallback",
    });
    mockUseSessionDetail.mockReturnValue({
      session: {
        ...LIVE_SESSION,
        summary_markdown: null,
        digest_markdown: null,
      },
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-live",
          role: "assistant",
          content: "Live transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    });

    render(<SessionsTab sessions={[catalogSession]} focusSessionId="live-1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Summary" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "## Catalog digest fallback",
    );
  });

  it("shows active/paused agent sessions in Live but excludes terminal agent statuses", async () => {
    render(
      <SessionsTab
        sessions={[
          RUNNING_AGENT_SESSION,
          PENDING_AGENT_SESSION,
          COMPLETED_AGENT_SESSION,
          ERRORED_AGENT_SESSION,
          CANCELLED_AGENT_SESSION,
        ]}
        focusSessionId="agent-running-1"
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("#206: Running Agent Terminal"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("#207: Pending Agent Terminal"),
      ).toBeInTheDocument();
    });

    expect(screen.queryByText("#208: Completed Agent Terminal")).toBeNull();
    expect(screen.queryByText("#209: Errored Agent Terminal")).toBeNull();
    expect(screen.queryByText("#210: Cancelled Agent Terminal")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "Expired" }));

    await waitFor(() => {
      expect(screen.queryByText("#206: Running Agent Terminal")).toBeNull();
      expect(screen.queryByText("#207: Pending Agent Terminal")).toBeNull();
    });
    expect(screen.queryByText("#208: Completed Agent Terminal")).toBeNull();
    expect(screen.queryByText("#209: Errored Agent Terminal")).toBeNull();
    expect(screen.queryByText("#210: Cancelled Agent Terminal")).toBeNull();
  });

  it("defaults to transcript mode, toggles to summary via digest fallback, and keeps action order", async () => {
    const onResumeSession = vi.fn();
    const onSwapSession = vi.fn();

    mockUseSessionDetail.mockReturnValue({
      session: {
        ...PAUSED_SESSION,
        summary_markdown: "   ",
        digest_markdown: "## Digest fallback",
      },
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: "Transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    });

    render(
      <SessionsTab
        sessions={[
          {
            ...PAUSED_SESSION,
            summary_markdown: "   ",
            digest_markdown: null,
          },
        ]}
        focusSessionId="paused-1"
        onResumeSession={onResumeSession}
        onSwapSession={onSwapSession}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Summary" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Resume" }),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Swap" })).toBeInTheDocument();
    });

    const summaryButton = screen.getByRole("button", { name: "Summary" });
    const resumeButton = screen.getByRole("button", { name: "Resume" });
    const swapButton = screen.getByRole("button", { name: "Swap" });

    expect(
      summaryButton.compareDocumentPosition(resumeButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      resumeButton.compareDocumentPosition(swapButton) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(summaryButton);

    expect(
      screen.getByRole("button", { name: "Transcript" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "## Digest fallback",
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(onResumeSession).toHaveBeenCalledWith("paused-1");

    fireEvent.click(screen.getByRole("button", { name: "Swap" }));
    expect(onSwapSession).toHaveBeenCalledWith({
      sessionId: "paused-1",
      sessionType: "terminal",
      agentRunId: null,
    });
    expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
    expect(screen.getByText("Transcript output")).toBeInTheDocument();
  });

  it("resets summary mode when focus targets the selected parked session", async () => {
    const onFocusHandled = vi.fn();
    const { rerender } = render(
      <SessionsTab
        sessions={[PAUSED_SESSION]}
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "# Session Summary",
    );

    rerender(
      <SessionsTab
        sessions={[PAUSED_SESSION]}
        focusSessionId="paused-1"
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Summary" }),
      ).toBeInTheDocument();
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
    });
    expect(onFocusHandled).toHaveBeenCalled();
  });

  it("shows the selected session transcript and follows session switches", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    mockUseSessionDetail.mockImplementation((sessionId) => ({
      session: sessionId === "live-1" ? LIVE_SESSION : PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: `msg-${sessionId ?? "none"}`,
          role: "assistant",
          content: `Transcript output for ${sessionId ?? "none"}`,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    render(<SessionsTab sessions={[LIVE_SESSION, PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for paused-1"),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("#201: Live Terminal"));

    await waitFor(() => {
      expect(
        screen.getByText("Transcript output for live-1"),
      ).toBeInTheDocument();
    });
  });

  it("loads newer transcript pages from the Virtuoso end edge when available", async () => {
    const loadNewer = vi.fn();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: "Transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
      hasMore: false,
      loadMore: vi.fn(),
      hasNewer: true,
      loadNewer,
      isLoadingOlder: false,
      isLoadingNewer: false,
      setTranscriptAtBottom: vi.fn(),
      firstItemIndex: 1_000_000,
      transcriptDegradedReason: null,
    });

    render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("virtuoso-end-reached"));

    expect(loadNewer).toHaveBeenCalledTimes(1);
  });

  it("does not load newer transcript pages when the window is already at the tail", async () => {
    const loadNewer = vi.fn();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: "Transcript output",
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
      hasMore: false,
      loadMore: vi.fn(),
      hasNewer: false,
      loadNewer,
      isLoadingOlder: false,
      isLoadingNewer: false,
      setTranscriptAtBottom: vi.fn(),
      firstItemIndex: 1_000_000,
      transcriptDegradedReason: null,
    });

    render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId("virtuoso-end-reached"));

    expect(loadNewer).not.toHaveBeenCalled();
  });

  it("renders the Watching bar without a duplicated session ref prefix (#19152)", async () => {
    const { container } = render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(
        container.querySelector(".activity-panel-status-bar__title"),
      ).toHaveTextContent("Watching Paused Terminal");
    });
    expect(
      container.querySelector(".activity-panel-status-bar__title"),
    ).not.toHaveTextContent("#202:");
  });

  it("re-renders the watching transcript when the last message grows in place", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    let transcriptContent = "abc";

    mockUseSessionDetail.mockImplementation(() => ({
      session: PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: transcriptContent,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    const { rerender } = render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("abc")).toBeInTheDocument();
    });

    transcriptContent = "abcdef";
    rerender(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("abcdef")).toBeInTheDocument();
    });
  });

  it("does not render transcript output while summary mode is active", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    let transcriptContent = "abc";

    mockUseSessionDetail.mockImplementation(() => ({
      session: PAUSED_SESSION,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [
        {
          id: "msg-1",
          role: "assistant",
          content: transcriptContent,
          timestamp: "2026-04-08T12:11:00Z",
        },
      ],
      isLoading: false,
      transcriptStatus: null,
    }));

    const { rerender } = render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("abc")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "# Session Summary",
    );

    transcriptContent = "abcdef";
    rerender(<SessionsTab sessions={[PAUSED_SESSION]} />);

    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "# Session Summary",
    );
    expect(screen.queryByText("abcdef")).toBeNull();
  });

  it("keeps session token accounting out of the sessions list UI", async () => {
    const highUsageSession = makeSession({
      id: "usage-1",
      ref: "#205",
      external_id: "usage-ext-1",
      title: "High Usage Session",
      seq_num: 205,
      usage_input_tokens: 1200,
      usage_output_tokens: 3400,
      updated_at: "2026-04-08T12:25:00Z",
    });

    render(
      <SessionsTab sessions={[highUsageSession]} focusSessionId="usage-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("#205: High Usage Session")).toBeInTheDocument();
    });

    expect(screen.queryByText("4.6K")).toBeNull();
    expect(screen.queryByText("1.2K / 3.4K")).toBeNull();
  });

  it("hides resume and swap for expired sessions without a transcript but keeps digest summary fallback when available", async () => {
    mockUseSessionDetail.mockReturnValue({
      session: {
        ...EXPIRED_SESSION,
        summary_markdown: "   ",
        digest_markdown: "## Expired digest fallback",
      },
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "missing" },
    });

    render(
      <SessionsTab
        sessions={[
          {
            ...EXPIRED_SESSION,
            summary_markdown: "   ",
            digest_markdown: null,
          },
        ]}
        onResumeSession={vi.fn()}
        onSwapSession={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("radio", { name: "Expired" }));

    await waitFor(() => {
      expect(screen.getByText("Session has no transcript")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Swap" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "## Expired digest fallback",
    );
  });

  it("shows an explicit empty state only when summary mode loses both summary and digest", async () => {
    let detailSession: GobbySession = {
      ...PAUSED_SESSION,
      summary_markdown: "# Initial summary",
      digest_markdown: null,
    };
    mockUseSessionDetail.mockImplementation(() => ({
      session: detailSession,
      sessionError: null,
      clearSessionError: vi.fn(),
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "missing" },
    }));

    const { rerender } = render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Summary" }),
      ).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "# Initial summary",
    );

    detailSession = {
      ...PAUSED_SESSION,
      summary_markdown: "   ",
      digest_markdown: "\n\t",
    };
    const emptyCatalogSession = {
      ...PAUSED_SESSION,
      summary_markdown: "   ",
      digest_markdown: "\n\t",
    };
    rerender(
      <SessionsTab
        sessions={[emptyCatalogSession]}
        focusSessionId="paused-1"
      />,
    );

    expect(screen.getByText("No summary available")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Summary" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Transcript" }),
    ).toBeInTheDocument();
  });

  it("disables Send Context with guidance when no web chat is active", async () => {
    render(<SessionsTab sessions={[PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Session actions" }));
    const sendContext = screen.getByRole("menuitem", { name: "Send Context" });

    expect(sendContext).toBeDisabled();
    expect(sendContext).toHaveAttribute(
      "title",
      "Start a web chat before sending context",
    );
  });

  it("enables Send Context when a web chat is active", async () => {
    render(
      <SessionsTab
        sessions={[PAUSED_SESSION]}
        chatSessionId="active-web-chat"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Session actions" }));

    expect(
      screen.getByRole("menuitem", { name: "Send Context" }),
    ).toBeEnabled();
  });

  it("open terminal focuses session", async () => {
    render(<TerminalFocusHarness />);

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Session actions" }));

    expect(screen.queryByRole("menuitem", { name: "Send Keys" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Capture Pane" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Open Terminal" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Attach #202 Paused Terminal" }),
      ).toHaveAttribute("aria-pressed", "true");
    });
    // Terminal is a regular activity tab: opening it switches the panel tab.
    expect(
      screen.getByRole("status", { name: "Active activity tab" }),
    ).toHaveTextContent("terminal");
    expect(terminalHook.attachSession).toHaveBeenCalledWith(
      "paused-pane",
      "default",
    );
    expect(
      screen.getByRole("status", { name: "Terminal focus request" }),
    ).toBeEmptyDOMElement();
  });

  it("restores a session in the list when expire fails", async () => {
    let resolveExpire: ((value: boolean) => void) | null = null;
    const onExpireSession = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveExpire = resolve;
        }),
    );

    render(
      <SessionsTab
        sessions={[PAUSED_SESSION]}
        onExpireSession={onExpireSession}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Session actions" }));
    expect(
      screen.getByRole("menuitem", { name: "Send Context" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send Command" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Expire Session" }));

    expect(onExpireSession).toHaveBeenCalledWith("paused-1");
    expect(screen.queryByText("#202: Paused Terminal")).toBeNull();

    await act(async () => {
      resolveExpire?.(false);
    });

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });
  });

  it("supports menu semantics, roving focus, and Escape focus restoration", async () => {
    render(<SessionsTab sessions={[PAUSED_SESSION]} />);

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });

    const trigger = screen.getByRole("button", { name: "Session actions" });
    expect(trigger).toHaveAttribute("aria-haspopup", "menu");
    fireEvent.click(trigger);

    const menu = screen.getByRole("menu", { name: "Session actions" });
    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toBeDisabled();
    expect(document.activeElement).toBe(items[1]);

    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[2]);

    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Session actions" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("lets users dismiss selected session detail errors", async () => {
    const clearSessionError = vi.fn();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
      sessionError: "Transcript is too large to display.",
      transcriptDownloadUrl: "/api/sessions/paused-1/transcript",
      clearSessionError,
      messages: [],
      isLoading: false,
      transcriptStatus: null,
    });

    render(
      <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
    );

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Transcript is too large to display.",
      );
    });
    expect(
      screen.getByRole("link", { name: "Download transcript instead" }),
    ).toHaveAttribute("href", "/api/sessions/paused-1/transcript");

    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss session error" }),
    );

    expect(clearSessionError).toHaveBeenCalledTimes(1);
  });

  describe("ACP capability-gated actions (#17400)", () => {
    const acpBlock = (caps: {
      resume: boolean;
      close: boolean;
      delete: boolean;
    }): NonNullable<GobbySession["acp"]> => ({
      capabilities: caps,
      additional_directories: [],
    });

    const makeAcpSession = (
      acp: GobbySession["acp"],
      overrides: Partial<GobbySession> = {},
    ): GobbySession =>
      makeSession({
        id: "acp-1",
        ref: "#301",
        external_id: "acp-ext-1",
        source: "qwen",
        title: "ACP Session",
        status: "active",
        session_type: "web_chat",
        seq_num: 301,
        updated_at: "2026-04-08T12:20:00Z",
        terminal_context: null,
        acp,
        ...overrides,
      });

    const openRowMenu = async () => {
      await waitFor(() => {
        expect(screen.getByText("#301: ACP Session")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "Session actions" }));
    };

    it("shows Resume/Close/Delete and hides Expire when all caps are advertised", async () => {
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: true, close: true, delete: true }),
            ),
          ]}
          onExpireSession={vi.fn(async () => true)}
          onResumeSession={vi.fn(async () => "new-id")}
          onAcpCloseSession={vi.fn(async () => true)}
          onAcpDeleteSession={vi.fn(async () => true)}
        />,
      );
      await openRowMenu();

      expect(
        screen.getByRole("menuitem", { name: "Resume Session" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("menuitem", { name: "Close Session" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("menuitem", { name: "Delete Session" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("menuitem", { name: "Expire Session" }),
      ).toBeNull();
    });

    it("invokes the resume handler from the menu", async () => {
      const onResumeSession = vi.fn(async () => "new-id");
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: true, close: false, delete: false }),
            ),
          ]}
          onResumeSession={onResumeSession}
        />,
      );
      await openRowMenu();

      fireEvent.click(screen.getByRole("menuitem", { name: "Resume Session" }));
      expect(onResumeSession).toHaveBeenCalledWith("acp-1");
    });

    it("optimistically hides the row on Close and restores it when close fails", async () => {
      let resolveClose: ((value: boolean) => void) | null = null;
      const onAcpCloseSession = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            resolveClose = resolve;
          }),
      );
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: false, close: true, delete: false }),
            ),
          ]}
          onAcpCloseSession={onAcpCloseSession}
        />,
      );
      await openRowMenu();

      fireEvent.click(screen.getByRole("menuitem", { name: "Close Session" }));
      expect(onAcpCloseSession).toHaveBeenCalledWith("acp-1");
      expect(screen.queryByText("#301: ACP Session")).toBeNull();

      await act(async () => {
        resolveClose?.(false);
      });
      await waitFor(() => {
        expect(screen.getByText("#301: ACP Session")).toBeInTheDocument();
      });
    });

    it("optimistically hides the row on Delete and restores it when delete fails", async () => {
      let resolveDelete: ((value: boolean) => void) | null = null;
      const onAcpDeleteSession = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            resolveDelete = resolve;
          }),
      );
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: false, close: false, delete: true }),
            ),
          ]}
          onAcpDeleteSession={onAcpDeleteSession}
        />,
      );
      await openRowMenu();

      fireEvent.click(screen.getByRole("menuitem", { name: "Delete Session" }));
      expect(onAcpDeleteSession).toHaveBeenCalledWith("acp-1");
      expect(screen.queryByText("#301: ACP Session")).toBeNull();

      await act(async () => {
        resolveDelete?.(false);
      });
      await waitFor(() => {
        expect(screen.getByText("#301: ACP Session")).toBeInTheDocument();
      });
    });

    it("shows only Close when only the close capability is advertised", async () => {
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: false, close: true, delete: false }),
            ),
          ]}
          onExpireSession={vi.fn(async () => true)}
          onAcpCloseSession={vi.fn(async () => true)}
        />,
      );
      await openRowMenu();

      expect(
        screen.getByRole("menuitem", { name: "Close Session" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("menuitem", { name: "Resume Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Delete Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Expire Session" }),
      ).toBeNull();
    });

    it("degrades to Send Context only when an ACP row advertises no capabilities", async () => {
      render(
        <SessionsTab
          sessions={[
            makeAcpSession(
              acpBlock({ resume: false, close: false, delete: false }),
            ),
          ]}
          onExpireSession={vi.fn(async () => true)}
        />,
      );
      await openRowMenu();

      expect(
        screen.getByRole("menuitem", { name: "Send Context" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("menuitem", { name: "Resume Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Close Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Delete Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Expire Session" }),
      ).toBeNull();
    });

    it("keeps Expire and omits ACP actions for non-ACP rows", async () => {
      render(
        <SessionsTab
          sessions={[PAUSED_SESSION]}
          onExpireSession={vi.fn(async () => true)}
          onAcpCloseSession={vi.fn(async () => true)}
          onAcpDeleteSession={vi.fn(async () => true)}
        />,
      );
      await waitFor(() => {
        expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("button", { name: "Session actions" }));

      expect(
        screen.getByRole("menuitem", { name: "Expire Session" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("menuitem", { name: "Close Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Delete Session" }),
      ).toBeNull();
      expect(
        screen.queryByRole("menuitem", { name: "Resume Session" }),
      ).toBeNull();
    });

    it("hides the detail-pane Resume button when the ACP resume capability is absent", async () => {
      const acpPaused = makeAcpSession(
        acpBlock({ resume: false, close: true, delete: false }),
        {
          id: "acp-paused-1",
          ref: "#302",
          status: "paused",
          seq_num: 302,
          title: "ACP Paused",
        },
      );
      mockUseSessionDetail.mockReturnValue({
        session: acpPaused,
        sessionError: null,
        clearSessionError: vi.fn(),
        messages: [
          {
            id: "m1",
            role: "assistant",
            content: "Transcript output",
            timestamp: "2026-04-08T12:26:00Z",
          },
        ],
        isLoading: false,
        transcriptStatus: null,
      });
      render(
        <SessionsTab
          sessions={[acpPaused]}
          focusSessionId="acp-paused-1"
          onResumeSession={vi.fn(async () => "new-id")}
        />,
      );
      await waitFor(() => {
        expect(screen.getByText("Transcript output")).toBeInTheDocument();
      });
      expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    });

    it("shows the detail-pane Resume button when the ACP resume capability is advertised", async () => {
      const acpPaused = makeAcpSession(
        acpBlock({ resume: true, close: false, delete: false }),
        {
          id: "acp-paused-1",
          ref: "#302",
          status: "paused",
          seq_num: 302,
          title: "ACP Paused",
        },
      );
      mockUseSessionDetail.mockReturnValue({
        session: acpPaused,
        sessionError: null,
        clearSessionError: vi.fn(),
        messages: [
          {
            id: "m1",
            role: "assistant",
            content: "Transcript output",
            timestamp: "2026-04-08T12:26:00Z",
          },
        ],
        isLoading: false,
        transcriptStatus: null,
      });
      render(
        <SessionsTab
          sessions={[acpPaused]}
          focusSessionId="acp-paused-1"
          onResumeSession={vi.fn(async () => "new-id")}
        />,
      );
      await waitFor(() => {
        expect(
          screen.getByRole("button", { name: "Resume" }),
        ).toBeInTheDocument();
      });
    });
  });
});
