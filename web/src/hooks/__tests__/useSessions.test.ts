import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

import { createMockFetch, type MockFetchInstance } from "../../test/mocks/fetch";

vi.mock("../useWebSocketEvent", () => ({
  useWebSocketEvent: vi.fn(),
}));

import { useSessionCatalog } from "../useSessionCatalog";
import { useWebSocketEvent } from "../useWebSocketEvent";
import {
  defaultSessionsFilters,
  matchesSessionsFilters,
} from "../../components/activity/sessionsFilters";

let mockFetch: MockFetchInstance;

const SAMPLE_SESSIONS = [
  {
    id: "sess-1",
    ref: "#100",
    external_id: "ext-1",
    source: "claude",
    project_id: "proj-1",
    title: "Test Session",
    status: "active",
    model: "claude-4",
    message_count: 5,
    created_at: "2026-03-01T00:00:00Z",
    updated_at: "2026-03-01T12:00:00Z",
    seq_num: 100,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: "main",
    usage_input_tokens: 1000,
    usage_output_tokens: 500,
    had_edits: true,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "web_chat",
    terminal_context: null,
  },
  {
    id: "sess-2",
    ref: "#101",
    external_id: "ext-2",
    source: "gemini",
    project_id: "proj-1",
    title: "Another Session",
    status: "expired",
    model: null,
    message_count: 10,
    created_at: "2026-03-02T00:00:00Z",
    updated_at: "2026-03-02T12:00:00Z",
    seq_num: 101,
    summary_markdown: null,
    digest_markdown: null,
    git_branch: null,
    usage_input_tokens: 2000,
    usage_output_tokens: 1000,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    agent_run_id: null,
    parent_session_id: null,
    session_type: "terminal",
    terminal_context: null,
  },
];

beforeEach(() => {
  mockFetch = createMockFetch();
  mockFetch.mockJsonResponse("/api/sessions", { sessions: SAMPLE_SESSIONS });
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  mockFetch.restore();
  vi.restoreAllMocks();
});

describe("useSessionCatalog", () => {
  it("fetches sessions on mount", async () => {
    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.sessions).toHaveLength(2);
    expect(result.current.sessions[0].title).toBe("Another Session");
  });

  it("passes the project filter and page-size limit to the sessions API", async () => {
    renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => {
      expect(
        mockFetch.fn.mock.calls.some(([url]) => {
          const u = String(url);
          return u.includes("/api/sessions") && u.includes("project_id=proj-1") && u.includes("limit=100");
        }),
      ).toBe(true);
    });
  });

  it("keeps expired and handoff_ready rows but hides deleted rows", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/sessions", {
      sessions: [
        ...SAMPLE_SESSIONS,
        {
          ...SAMPLE_SESSIONS[0],
          id: "sess-handoff",
          status: "handoff_ready",
          updated_at: "2026-03-03T12:00:00Z",
        },
        { ...SAMPLE_SESSIONS[0], id: "sess-deleted", status: "deleted" },
      ],
    });

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.sessions.map((session) => session.id)).toEqual([
      "sess-handoff",
      "sess-2",
      "sess-1",
    ]);
  });

  it("handles fetch error gracefully", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockErrorResponse("/api/sessions", 500);

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.error).toBeTruthy();
    expect(result.current.sessions).toHaveLength(0);
  });

  it("markSessionDeleting tracks a pending delete", async () => {
    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    act(() => result.current.markSessionDeleting("sess-1"));

    expect(result.current.deletingIds.has("sess-1")).toBe(true);
  });

  it("confirmSessionDeleted removes the session and clears deletingIds", async () => {
    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    act(() => {
      result.current.markSessionDeleting("sess-1");
      result.current.confirmSessionDeleted("sess-1");
    });

    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.deletingIds.has("sess-1")).toBe(false);
  });

  it("renameSession updates title optimistically", async () => {
    mockFetch.mockJsonResponse("/api/sessions/sess-1/rename", { ok: true });

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    await act(async () => {
      await result.current.renameSession("sess-1", "Renamed");
    });

    expect(
      result.current.sessions.find((session) => session.id === "sess-1")?.title,
    ).toBe("Renamed");
  });

  it("hasMore reflects next_cursor on the response", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/sessions", {
      sessions: SAMPLE_SESSIONS,
      next_cursor: { updated_at: "2026-03-01T12:00:00Z", id: "sess-1" },
    });

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.hasMore).toBe(true);
  });

  it("hasMore is false when next_cursor is null", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/sessions", {
      sessions: SAMPLE_SESSIONS,
      next_cursor: null,
    });

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.hasMore).toBe(false);
  });

  it("loadMore appends the next page using the cursor", async () => {
    mockFetch.resetRoutes();
    let callCount = 0;
    mockFetch.fn.mockImplementation(async (url) => {
      callCount += 1;
      const u = String(url);
      const isFirstPage = !u.includes("cursor_updated_at");
      const body = isFirstPage
        ? {
            sessions: [SAMPLE_SESSIONS[0]],
            next_cursor: { updated_at: "2026-03-01T12:00:00Z", id: "sess-1" },
          }
        : {
            sessions: [SAMPLE_SESSIONS[1]],
            next_cursor: null,
          };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.sessions).toHaveLength(1));
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.sessions).toHaveLength(2);
    expect(result.current.hasMore).toBe(false);
    expect(callCount).toBe(2);
  });

  it("session_event with event=session_expired patches the catalog and Live filter drops the row", async () => {
    const mockedUseWS = vi.mocked(useWebSocketEvent);
    mockedUseWS.mockClear();

    const { result } = renderHook(() => useSessionCatalog("proj-1"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    const before = result.current.sessions.find((s) => s.id === "sess-1");
    expect(before?.status).toBe("active");

    const liveFilter = defaultSessionsFilters();
    const now = new Date("2026-03-01T13:00:00Z");
    expect(matchesSessionsFilters(before!, liveFilter, now)).toBe(true);

    const sessionEventRegistration = mockedUseWS.mock.calls.find(
      ([type]) => type === "session_event",
    );
    expect(sessionEventRegistration).toBeDefined();
    const sessionEventHandler = sessionEventRegistration![1];

    act(() => {
      sessionEventHandler({
        type: "session_event",
        event: "session_expired",
        session_id: "sess-1",
      });
    });

    const after = result.current.sessions.find((s) => s.id === "sess-1");
    expect(after?.status).toBe("expired");
    expect(matchesSessionsFilters(after!, liveFilter, now)).toBe(false);
  });

  it("session_event with event=session_deleted removes the session from the catalog", async () => {
    const mockedUseWS = vi.mocked(useWebSocketEvent);
    mockedUseWS.mockClear();

    const { result } = renderHook(() => useSessionCatalog("proj-1"));
    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(result.current.sessions.some((s) => s.id === "sess-2")).toBe(true);

    const sessionEventRegistration = mockedUseWS.mock.calls.find(
      ([type]) => type === "session_event",
    );
    const sessionEventHandler = sessionEventRegistration![1];

    act(() => {
      sessionEventHandler({
        type: "session_event",
        event: "session_deleted",
        session_id: "sess-2",
      });
    });

    expect(result.current.sessions.some((s) => s.id === "sess-2")).toBe(false);
  });

  it("renameSession restores the previous title when the API call fails", async () => {
    const { result } = renderHook(() => useSessionCatalog("proj-1"));

    await waitFor(() => expect(result.current.sessions).toHaveLength(2));

    mockFetch.resetRoutes();
    mockFetch.mockErrorResponse("/api/sessions/sess-1/rename", 500);
    mockFetch.mockErrorResponse("/api/sessions", 500);

    await act(async () => {
      await result.current.renameSession("sess-1", "Renamed");
    });

    expect(
      result.current.sessions.find((session) => session.id === "sess-1")?.title,
    ).toBe("Test Session");
  });
});
