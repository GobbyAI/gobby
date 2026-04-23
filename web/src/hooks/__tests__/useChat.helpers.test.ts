import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import {
  cleanupUseChatTestContext,
  createUseChatTestContext,
  loadUseChatModule,
  type UseChatTestContext,
} from "./useChat.setup";

let context: UseChatTestContext;
let mockFetch: UseChatTestContext["mockFetch"];
let useChat: Awaited<ReturnType<typeof loadUseChatModule>>;

beforeEach(() => {
  context = createUseChatTestContext();
  mockFetch = context.mockFetch;
});

afterEach(() => {
  cleanupUseChatTestContext(context);
});

async function loadModule() {
  useChat = await loadUseChatModule();
}

describe("useChat persisted message helpers", () => {
  it("restores persisted chat messages as string content on mount", async () => {
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        source: "claude",
        session_type: "web_chat",
        status: "active",
      },
    });
    mockFetch.mockJsonResponse("/api/chat/test-conversation-id/messages", {
      messages: [
        {
          id: "restored-1",
          role: "assistant",
          content: "Restored output",
          tool_calls: [],
          seq: 1,
          created_at: "2026-04-14T00:00:00Z",
        },
      ],
      max_seq: 1,
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.messages).toHaveLength(1);
      expect(result.current.messages[0].content).toBe("Restored output");
      expect(result.current.messages[0].contentBlocks?.[0]).toEqual({
        type: "text",
        content: "Restored output",
      });
    });
  });

  it("restores protocol-tagged raw chat rows as system messages", async () => {
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        source: "claude",
        session_type: "web_chat",
        status: "active",
      },
    });
    mockFetch.mockJsonResponse("/api/chat/test-conversation-id/messages", {
      messages: [
        {
          id: "restored-protocol-1",
          role: "user",
          content:
            '<local-command-caveat><command>npm test</command></local-command-caveat>',
          tool_calls: [],
          seq: 1,
          created_at: "2026-04-14T00:00:00Z",
        },
      ],
      max_seq: 1,
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.messages).toHaveLength(1);
      expect(result.current.messages[0].role).toBe("system");
    });
  });

  it("restores persisted web-chat sessions with unknown non-terminal statuses", async () => {
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        source: "claude",
        session_type: "web_chat",
        status: "resuming",
      },
    });
    mockFetch.mockJsonResponse("/api/chat/test-conversation-id/messages", {
      messages: [
        {
          id: "restored-unknown-status",
          role: "assistant",
          content: "Recovered after resume",
          tool_calls: [],
          seq: 1,
          created_at: "2026-04-14T00:00:00Z",
        },
      ],
      max_seq: 1,
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.messages).toHaveLength(1);
      expect(result.current.messages[0].content).toBe("Recovered after resume");
    });
  });

  it("does not restore persisted web-chat sessions in terminal statuses", async () => {
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        source: "claude",
        session_type: "web_chat",
        status: "expired",
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.dbSessionId).toBeNull();
      expect(result.current.conversationId).toBe("");
    });

    expect(localStorage.removeItem).toHaveBeenCalledWith("gobby-db-session-id");
    expect(localStorage.removeItem).toHaveBeenCalledWith("gobby-conversation-id");
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes("/api/chat/test-conversation-id/messages"),
      ),
    ).toBe(false);
  });

  it("rejects persisted terminal session ids for main-chat restore", async () => {
    localStorage.setItem("gobby-conversation-id", "terminal-session");
    localStorage.setItem("gobby-db-session-id", "terminal-session");

    mockFetch.mockJsonResponse("/api/sessions/terminal-session", {
      session: {
        id: "terminal-session",
        source: "codex",
        session_type: "terminal",
        status: "active",
        title: "Terminal Session",
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.dbSessionId).toBeNull();
      expect(result.current.conversationId).toBe("");
    });

    expect(localStorage.removeItem).toHaveBeenCalledWith("gobby-db-session-id");
    expect(localStorage.removeItem).toHaveBeenCalledWith("gobby-conversation-id");
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes("/api/chat/terminal-session/messages"),
      ),
    ).toBe(false);
  });

  it("hydrates persisted main-session metadata from the durable db session on mount", async () => {
    mockFetch.mockJsonResponse("/api/chat/test-conversation-id/messages?limit=100&after_seq=0", {
      messages: [],
      max_seq: 0,
    });
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        seq_num: 314,
        title: "Persisted Main Chat",
        source: "codex",
        session_type: "web_chat",
        chat_mode: "bypass",
        git_branch: "feature/mobile-refresh",
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        context_window: null,
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    await waitFor(() => {
      expect(result.current.sessionTitle).toBe("Persisted Main Chat");
      expect(result.current.sessionRef).toBe("#314");
      expect(result.current.currentBranch).toBe("feature/mobile-refresh");
      expect(result.current.selectedProvider).toBe("codex");
    });
  });
});
