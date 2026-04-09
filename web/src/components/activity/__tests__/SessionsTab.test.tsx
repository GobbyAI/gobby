import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SessionsTab } from "../SessionsTab";
import { createMockFetch, type MockFetchInstance } from "../../../test/mocks/fetch";

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../shared/SourceIcon", () => ({
  SourceIcon: ({ source }: { source: string }) => (
    <span data-testid="source-icon">{source}</span>
  ),
}));

vi.mock("../../../hooks/useSessionDetail", () => ({
  useSessionDetail: () => ({ messages: [], isLoading: false }),
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
    usage_total_cost_usd: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: { session_name: "dev" },
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
    usage_total_cost_usd: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
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
    usage_total_cost_usd: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: "plan",
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
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
    usage_total_cost_usd: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
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
    usage_total_cost_usd: 0,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
  },
];

describe("SessionsTab", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
    mockFetch.mockJsonResponse("/api/sessions?status=active", {
      sessions: activeSessions,
    });
    mockFetch.mockJsonResponse("/api/sessions?status=paused", { sessions: [] });
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
    });

    expect(screen.queryByText("#202: Current Web Chat")).toBeNull();
    expect(screen.queryByText("#204: Pipeline Session")).toBeNull();
    expect(screen.queryByText("#205: Cron Session")).toBeNull();

    expect(screen.getAllByText(/^tmux$/i)).toHaveLength(1);
    expect(screen.getAllByText(/^web$/i)).toHaveLength(1);
  });

  it("shows mode badges and lets a watched web chat attach into the main chat", async () => {
    const onAttachSession = vi.fn();

    render(
      <SessionsTab
        chatSessionId="web-current"
        focusSessionId="web-other"
        onAttachSession={onAttachSession}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/^plan$/i)).toBeTruthy();
      expect(screen.getByText("Attach")).toBeTruthy();
    });

    fireEvent.click(screen.getByText("Attach"));

    expect(onAttachSession).toHaveBeenCalledWith("web-other");
  });
});
