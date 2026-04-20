import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SessionsTab } from "../SessionsTab";
import { createMockFetch, type MockFetchInstance } from "../../../test/mocks/fetch";
import type { GobbySession } from "../../../types/sessions";

type SessionDetailMock = {
  session: GobbySession | null;
  messages: Array<{ id?: string; role?: string; content?: string; timestamp?: string }>;
  isLoading: boolean;
  transcriptStatus: { content_state: string } | null;
};

const mockUseSessionDetail = vi.fn<() => SessionDetailMock>(() => ({
  session: null,
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
    mockUseSessionDetail.mockReset();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
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
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("filters live vs expired, excludes pipeline sources, and searches title/ref/external id", async () => {
    render(
      <SessionsTab
        sessions={[LIVE_SESSION, PAUSED_SESSION, EXPIRED_SESSION, PIPELINE_SESSION]}
        focusSessionId="live-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
      expect(screen.getByText("#201: Live Terminal")).toBeInTheDocument();
    });

    expect(screen.queryByText("#203: Expired Terminal")).toBeNull();
    expect(screen.queryByText("#204: Pipeline Session")).toBeNull();

    fireEvent.change(screen.getByPlaceholderText("Search sessions"), {
      target: { value: "paused-ext-1" },
    });
    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
      expect(screen.queryByText("#201: Live Terminal")).toBeNull();
    });

    fireEvent.change(screen.getByPlaceholderText("Search sessions"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("Session status filter"), {
      target: { value: "expired" },
    });

    await waitFor(() => {
      expect(screen.getByText("#203: Expired Terminal")).toBeInTheDocument();
    });
    expect(screen.queryByText("#202: Paused Terminal")).toBeNull();
  });

  it("defaults to transcript mode, toggles to summary via digest fallback, and keeps action order", async () => {
    const onResumeSession = vi.fn();
    const onSwapSession = vi.fn();

    mockUseSessionDetail.mockReturnValue({
      session: {
        ...PAUSED_SESSION,
        summary_markdown: null,
        digest_markdown: "## Digest fallback",
      },
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
        sessions={[PAUSED_SESSION]}
        focusSessionId="paused-1"
        onResumeSession={onResumeSession}
        onSwapSession={onSwapSession}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Transcript output")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
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

    expect(screen.getByRole("button", { name: "Transcript" })).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "Transcript" }));
    expect(screen.getByText("Transcript output")).toBeInTheDocument();
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

    render(<SessionsTab sessions={[highUsageSession]} focusSessionId="usage-1" />);

    await waitFor(() => {
      expect(screen.getByText("#205: High Usage Session")).toBeInTheDocument();
    });

    expect(screen.queryByText("4.6K")).toBeNull();
    expect(screen.queryByText("1.2K / 3.4K")).toBeNull();
  });

  it("hides resume and swap for expired sessions without a transcript but keeps summary when available", async () => {
    mockUseSessionDetail.mockReturnValue({
      session: EXPIRED_SESSION,
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "missing" },
    });

    render(
      <SessionsTab
        sessions={[EXPIRED_SESSION]}
        onResumeSession={vi.fn()}
        onSwapSession={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByLabelText("Session status filter"), {
      target: { value: "expired" },
    });

    await waitFor(() => {
      expect(screen.getByText("Session has no transcript")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Swap" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
      "# Expired Summary",
    );
  });

  it("shows an explicit empty state when summary and digest are both missing", async () => {
    mockUseSessionDetail.mockReturnValue({
      session: { ...PAUSED_SESSION, summary_markdown: null, digest_markdown: null },
      messages: [],
      isLoading: false,
      transcriptStatus: { content_state: "missing" },
    });

    render(
      <SessionsTab
        sessions={[PAUSED_SESSION]}
        focusSessionId="paused-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Summary" }));
    expect(screen.getByText("No summary available")).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Expire Session" }));

    expect(onExpireSession).toHaveBeenCalledWith("paused-1");
    expect(screen.queryByText("#202: Paused Terminal")).toBeNull();

    await act(async () => {
      resolveExpire?.(false);
    });

    await waitFor(() => {
      expect(screen.getByText("#202: Paused Terminal")).toBeInTheDocument();
    });
  });
});
