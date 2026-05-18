import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { SessionsTab } from "../SessionsTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import type { GobbySession } from "../../../types/sessions";
import type { SessionMessage } from "../../../hooks/useSessionDetail";

type SessionDetailMock = {
  session: GobbySession | null;
  sessionError: string | null;
  clearSessionError: () => void;
  messages: SessionMessage[];
  isLoading: boolean;
  transcriptStatus: { content_state: string } | null;
};

const mockUseSessionDetail = vi.fn<
  (sessionId?: string | null) => SessionDetailMock
>(() => ({
  session: null,
  sessionError: null,
  clearSessionError: vi.fn(),
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

function mockElementScrollIntoView() {
  const scrollIntoView = vi.fn();
  const originalDescriptor = Object.getOwnPropertyDescriptor(
    Element.prototype,
    "scrollIntoView",
  );
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: scrollIntoView,
  });

  return {
    scrollIntoView,
    restore() {
      if (originalDescriptor) {
        Object.defineProperty(
          Element.prototype,
          "scrollIntoView",
          originalDescriptor,
        );
      } else {
        delete (Element.prototype as { scrollIntoView?: unknown })
          .scrollIntoView;
      }
    },
  };
}

describe("SessionsTab", () => {
  beforeEach(() => {
    // SessionsTab persists the watched session id in localStorage. Without
    // clearing, a prior test's selection leaks into the next one and trips
    // selectedEntry-gated UI (Summary/Resume/Swap buttons render against null).
    localStorage.clear();
    mockUseSessionDetail.mockReset();
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
    });
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse("/api/agents/running", { agents: [] });
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
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
    expect(screen.queryByText("#202: Paused Terminal")).toBeNull();
    expect(screen.queryByText("#205: Handoff Terminal")).toBeNull();
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
        sessionId === "main-web-1" ? MAIN_WEB_CHAT_SESSION : PARKED_WEB_CHAT_SESSION,
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
      expect(screen.getAllByText("#211: Parked Web Chat").length).toBeGreaterThan(0);
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
      expect(screen.getAllByText("#210: Main Web Chat").length).toBeGreaterThan(0);
      expect(
        screen.getByText("Transcript output for main-web-1"),
      ).toBeInTheDocument();
    });
    expect(screen.getAllByText("#211: Parked Web Chat").length).toBeGreaterThan(0);
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
      expect(screen.getAllByText("#201: Live Terminal").length).toBeGreaterThan(0);
      expect(screen.getAllByText("#202: Paused Terminal").length).toBeGreaterThan(
        0,
      );
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
      "activity-row-status-dot--pulse",
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

    render(
      <SessionsTab
        sessions={[LIVE_SESSION]}
        focusSessionId="live-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("Live transcript output")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
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

    render(
      <SessionsTab
        sessions={[catalogSession]}
        focusSessionId="live-1"
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Summary" })).toBeInTheDocument();
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

  it("scrolls the watching transcript to bottom when selecting another session", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    const { scrollIntoView, restore } = mockElementScrollIntoView();
    try {
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
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
      });
      expect(scrollIntoView).toHaveBeenLastCalledWith({
        behavior: "auto",
        block: "end",
      });
      expect(
        screen
          .getByText("Transcript output for paused-1")
          .closest(".chat-scaled"),
      ).toHaveStyle({
        scrollBehavior: "auto",
        overflowAnchor: "none",
        overscrollBehavior: "contain",
      });

      fireEvent.click(screen.getByText("#201: Live Terminal"));

      await waitFor(() => {
        expect(
          screen.getByText("Transcript output for live-1"),
        ).toBeInTheDocument();
      });
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
      expect(scrollIntoView).toHaveBeenLastCalledWith({
        behavior: "auto",
        block: "end",
      });
    } finally {
      restore();
    }
  });

  it("scrolls the watching transcript when the last message grows in place", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    const { scrollIntoView, restore } = mockElementScrollIntoView();
    let transcriptContent = "abc";

    try {
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
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
      });

      transcriptContent = "abcdef";
      rerender(
        <SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />,
      );

      await waitFor(() => {
        expect(screen.getByText("abcdef")).toBeInTheDocument();
        expect(scrollIntoView).toHaveBeenCalledTimes(2);
      });
      expect(scrollIntoView).toHaveBeenLastCalledWith({
        behavior: "auto",
        block: "end",
      });
    } finally {
      restore();
    }
  });

  it("does not scroll transcript output while summary mode is active", async () => {
    localStorage.removeItem("gobby-watching-session-id");
    const { scrollIntoView, restore } = mockElementScrollIntoView();
    let transcriptContent = "abc";

    try {
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
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
      });

      fireEvent.click(screen.getByRole("button", { name: "Summary" }));
      expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
        "# Session Summary",
      );

      scrollIntoView.mockClear();
      transcriptContent = "abcdef";
      rerender(<SessionsTab sessions={[PAUSED_SESSION]} />);

      expect(screen.getByTestId("summary-markdown")).toHaveTextContent(
        "# Session Summary",
      );
      expect(screen.queryByText("abcdef")).toBeNull();
      expect(scrollIntoView).not.toHaveBeenCalled();
    } finally {
      restore();
    }
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
      <SessionsTab sessions={[emptyCatalogSession]} focusSessionId="paused-1" />,
    );

    expect(screen.getByText("No summary available")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Summary" })).toBeNull();
    expect(screen.getByRole("button", { name: "Transcript" })).toBeInTheDocument();
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

  it("lets users dismiss selected session detail errors", async () => {
    const clearSessionError = vi.fn();
    mockUseSessionDetail.mockReturnValue({
      session: PAUSED_SESSION,
      sessionError: "Failed to load session detail",
      clearSessionError,
      messages: [],
      isLoading: false,
      transcriptStatus: null,
    });

    render(<SessionsTab sessions={[PAUSED_SESSION]} focusSessionId="paused-1" />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Failed to load session detail",
      );
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss session error" }),
    );

    expect(clearSessionError).toHaveBeenCalledTimes(1);
  });
});
