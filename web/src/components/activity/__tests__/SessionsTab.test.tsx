import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SessionsTab } from "../SessionsTab";
import { createMockFetch, type MockFetchInstance } from "../../../test/mocks/fetch";

type SessionDetailMock = {
  messages: Array<{ id?: string; content?: string }>
  isLoading: boolean
  transcriptStatus: { content_state: string } | null
}

const mockUseSessionDetail = vi.fn<() => SessionDetailMock>(() => ({
  messages: [],
  isLoading: false,
  transcriptStatus: null,
}));

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../shared/SourceIcon", () => ({
  SourceIcon: ({ source }: { source: string }) => (
    <span data-testid="source-icon">{source}</span>
  ),
}));

vi.mock("../../../hooks/useSessionDetail", () => ({
  useSessionDetail: () => mockUseSessionDetail(),
}));

vi.mock("../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn(async () => true),
    ConfirmDialogElement: <div data-testid="confirm-dialog" />,
  }),
}));

vi.mock("../../chat/MessageItem", () => ({
  MessageItem: ({ message }: { message: { content: string } }) => (
    <div data-testid="message-item">{message.content}</div>
  ),
}));

vi.mock("../SessionInteractionModal", () => ({
  SessionInteractionModal: () => null,
}));

let mockFetch: MockFetchInstance;

const activeSessions = [
  {
    id: "terminal-1",
    ref: "#201",
    external_id: "terminal-ext-1",
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
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { session_name: "dev" },
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
  {
    id: "web-current",
    ref: "#202",
    external_id: "web-current-ext",
    source: "claude",
    project_id: "proj-1",
    title: "Current Web Chat",
    status: "active",
    model: "sonnet",
    message_count: 8,
    created_at: "2026-04-08T12:10:00Z",
    updated_at: "2026-04-08T12:15:00Z",
    seq_num: 202,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  },
  {
    id: "web-other",
    ref: "#203",
    external_id: "web-other-ext",
    source: "codex",
    project_id: "proj-1",
    title: "Other Web Chat",
    status: "active",
    model: "gpt-5.4",
    message_count: 11,
    created_at: "2026-04-08T12:20:00Z",
    updated_at: "2026-04-08T12:25:00Z",
    seq_num: 203,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: "run-auto-203",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
    sandbox_enabled: true,
    sandbox_policy_hash: "policy-web",
  },
  {
    id: "pipeline-1",
    ref: "#204",
    external_id: "pipeline-ext-1",
    source: "pipeline",
    project_id: "proj-1",
    title: "Pipeline Session",
    status: "active",
    model: null,
    message_count: 0,
    created_at: "2026-04-08T12:30:00Z",
    updated_at: "2026-04-08T12:35:00Z",
    seq_num: 204,
    summary_markdown: null,
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
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
  {
    id: "cron-1",
    ref: "#205",
    external_id: "cron-ext-1",
    source: "cron",
    project_id: "proj-1",
    title: "Cron Session",
    status: "active",
    model: null,
    message_count: 0,
    created_at: "2026-04-08T12:40:00Z",
    updated_at: "2026-04-08T12:45:00Z",
    seq_num: 205,
    summary_markdown: null,
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
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
];

const pausedSessions = [
  {
    id: "terminal-paused-live",
    ref: "#206",
    external_id: "terminal-paused-live-ext",
    source: "codex",
    project_id: "proj-1",
    title: "Paused Terminal Session",
    status: "paused",
    model: "gpt-5.4",
    message_count: 5,
    created_at: "2026-04-08T12:50:00Z",
    updated_at: "2026-04-08T12:55:00Z",
    seq_num: 206,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { tmux_pane: "%44", parent_pid: 1234 },
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
  {
    id: "terminal-paused-stale",
    ref: "#207",
    external_id: "terminal-paused-stale-ext",
    source: "codex",
    project_id: "proj-1",
    title: "Stale Terminal Session",
    status: "paused",
    model: "gpt-5.4",
    message_count: 5,
    created_at: "2026-04-07T12:50:00Z",
    updated_at: "2026-04-08T12:55:00Z",
    seq_num: 207,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
];

const handoffReadySessions = [
  {
    id: "terminal-handoff-live",
    ref: "#208",
    external_id: "terminal-handoff-live-ext",
    source: "qwen",
    project_id: "proj-1",
    title: "Live Handoff Session",
    status: "handoff_ready",
    model: "qwen3-coder",
    message_count: 4,
    created_at: "2026-04-08T13:00:00Z",
    updated_at: "2026-04-08T13:05:00Z",
    seq_num: 208,
    summary_markdown: null,
    git_branch: "main",
    usage_input_tokens: 0,
    usage_output_tokens: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { tmux_pane: "%45", parent_pid: 4321 },
    can_proxy_attach: true,
    sandbox_enabled: false,
    sandbox_policy_hash: null,
  },
];

describe("SessionsTab", () => {
  beforeEach(() => {
    mockUseSessionDetail.mockReset();
    mockUseSessionDetail.mockReturnValue({
      messages: [],
      isLoading: false,
      transcriptStatus: null,
    });
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockFetch.mockJsonResponse("/api/sessions?status=active", {
      sessions: activeSessions,
    });
    mockFetch.mockJsonResponse("/api/sessions?status=paused", { sessions: [] });
    mockFetch.mockJsonResponse("/api/sessions?status=handoff_ready", {
      sessions: [],
    });
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("shows other web chats, hides the current chat, and preserves session chips", async () => {
    render(<SessionsTab chatSessionId="web-current" />);

    await waitFor(() => {
      expect(screen.getByText("#201: Terminal Session")).toBeTruthy();
      expect(screen.getByText("#203: Other Web Chat")).toBeTruthy();
      expect(screen.getByText(/Watching #201: Terminal Session/)).toBeTruthy();
    });

    expect(screen.queryByText("#202: Current Web Chat")).toBeNull();
    expect(screen.queryByText("#204: Pipeline Session")).toBeNull();
    expect(screen.queryByText("#205: Cron Session")).toBeNull();

    expect(screen.getAllByText(/^tmux$/i)).toHaveLength(1);
    expect(screen.getAllByText(/^web$/i)).toHaveLength(1);
    expect(screen.getAllByText(/^SB$/i)).toHaveLength(1);
    expect(screen.getAllByLabelText("Session actions")).toHaveLength(2);
    expect(screen.queryByText("Close")).toBeNull();
  });

  it("hides paused terminal sessions that no longer have terminal liveness metadata", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockFetch.mockJsonResponse("/api/sessions?status=active", {
      sessions: activeSessions,
    });
    mockFetch.mockJsonResponse("/api/sessions?status=paused", {
      sessions: pausedSessions,
    });
    mockFetch.mockJsonResponse("/api/sessions?status=handoff_ready", {
      sessions: [],
    });

    render(<SessionsTab chatSessionId="web-current" />);

    await waitFor(() => {
      expect(screen.getByText("#206: Paused Terminal Session")).toBeTruthy();
    });

    expect(screen.queryByText("#207: Stale Terminal Session")).toBeNull();
  });

  it("keeps live handoff-ready tmux sessions available in the activity pane", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockFetch.mockJsonResponse("/api/sessions?status=active", {
      sessions: activeSessions,
    });
    mockFetch.mockJsonResponse("/api/sessions?status=paused", {
      sessions: [],
    });
    mockFetch.mockJsonResponse("/api/sessions?status=handoff_ready", {
      sessions: handoffReadySessions,
    });

    render(<SessionsTab chatSessionId="web-current" />);

    await waitFor(() => {
      expect(screen.getByText("#208: Live Handoff Session")).toBeTruthy();
    });

    expect(screen.getByText(/Watching #201: Terminal Session/)).toBeTruthy();
  });

  it("shows agent badge and lets a watched session swap into the main chat", async () => {
    const onSwapSession = vi.fn();

    render(
      <SessionsTab
        chatSessionId="web-current"
        focusSessionId="web-other"
        onSwapSession={onSwapSession}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/^auto$/i)).toBeTruthy();
      expect(screen.getByText("Swap")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Swap"));

    expect(onSwapSession).toHaveBeenCalledWith({
      sessionId: "web-other",
      sessionType: "web_chat",
      agentRunId: "run-auto-203",
    });
    expect(screen.getByText(/Watching #203: Other Web Chat/)).toBeTruthy();
  });

  it("keeps the watched pane open when the selected session is clicked again", async () => {
    render(<SessionsTab chatSessionId="web-current" />);

    await waitFor(() => {
      expect(screen.getByText("#201: Terminal Session")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("#201: Terminal Session"));
    expect(screen.getByText(/Watching #201: Terminal Session/)).toBeTruthy();

    fireEvent.click(screen.getByText("#201: Terminal Session"));
    expect(screen.getByText(/Watching #201: Terminal Session/)).toBeTruthy();
  });

  it("clears the watched pane when the selected session becomes the main chat without a parked replacement", async () => {
    const { rerender } = render(
      <SessionsTab focusSessionId="web-other" />,
    );

    await waitFor(() => {
      expect(screen.getByText(/Watching #203: Other Web Chat/)).toBeTruthy();
    });

    rerender(<SessionsTab chatSessionId="web-other" />);

    await waitFor(() => {
      expect(screen.queryByText(/Watching /)).toBeNull();
    });
  });

  it("shows parser mismatch empty state for unparseable transcripts", async () => {
    mockUseSessionDetail.mockReturnValue({
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "unparseable" },
    });

    render(<SessionsTab focusSessionId="terminal-1" />);

    await waitFor(() => {
      expect(screen.getByText("Transcript exists but could not be parsed")).toBeTruthy();
    });
  });

  it("shows missing transcript empty state", async () => {
    mockUseSessionDetail.mockReturnValue({
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "missing" },
    });

    render(<SessionsTab focusSessionId="terminal-1" />);

    await waitFor(() => {
      expect(screen.getByText("Session has no transcript")).toBeTruthy();
    });
  });

  it("only handles focus once when the focused session is already selected", async () => {
    const onFocusHandled = vi.fn();

    render(
      <SessionsTab
        focusSessionId="terminal-1"
        onFocusHandled={onFocusHandled}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#201: Terminal Session")).toBeTruthy();
      expect(onFocusHandled).toHaveBeenCalledTimes(1);
    });
  });
});
