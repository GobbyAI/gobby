import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  cleanupUseChatTestContext,
  createUseChatTestContext,
  loadUseChatModule,
  type UseChatTestContext,
} from "./useChat.setup";

let context: UseChatTestContext;
let mockWs: UseChatTestContext["mockWs"];
let mockFetch: UseChatTestContext["mockFetch"];
let useChat: Awaited<ReturnType<typeof loadUseChatModule>>;

beforeEach(() => {
  context = createUseChatTestContext();
  mockWs = context.mockWs;
  mockFetch = context.mockFetch;
});

afterEach(() => {
  cleanupUseChatTestContext(context);
});

async function loadModule() {
  useChat = await loadUseChatModule();
}

describe("useChat message and conversation state", () => {
  it("generates a conversation ID on mount", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    expect(result.current.conversationId).toBeTruthy();
    expect(typeof result.current.conversationId).toBe("string");
  });

  it("persists conversation ID to localStorage on send", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));

    // Should have saved the conversation ID when sending
    expect(localStorage.setItem).toHaveBeenCalledWith(
      "gobby-conversation-id",
      expect.any(String),
    );
  });

  it("keeps conversation and db session storage separate when resuming an external session", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    act(() => {
      result.current.resumeSession("claude-ext-456");
    });

    expect(localStorage.getItem("gobby-conversation-id")).toBe(
      "claude-ext-456",
    );
    expect(localStorage.getItem("gobby-db-session-id")).toBe(
      "test-conversation-id",
    );
  });

  it("resets reconnect backfill when the active chat identity changes", async () => {
    vi.useFakeTimers();
    try {
      await loadModule();
      mockFetch.mockJsonResponse(
        "/api/chat/db-session-2/messages?limit=100&after_seq=0",
        {
          messages: [],
          max_seq: 5,
        },
      );
      mockFetch.mockJsonResponse("/api/sessions/db-session-2", {
        session: {
          id: "db-session-2",
          seq_num: 202,
          title: "Other chat",
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          usage_cache_read_tokens: 0,
          usage_cache_creation_tokens: 0,
          context_window: null,
        },
      });

      const { result } = renderHook(() => useChat());
      const ws = mockWs.instances[0];
      act(() => ws.simulateOpen());

      await act(async () => {
        result.current.switchConversation("db-session-2");
        await Promise.resolve();
        await Promise.resolve();
      });

      act(() => {
        result.current.resumeSession("claude-ext-789");
      });

      act(() => {
        ws.simulateClose();
        vi.advanceTimersByTime(2000);
      });

      expect(mockWs.instances).toHaveLength(2);

      await act(async () => {
        mockWs.instances[1].simulateOpen();
        await Promise.resolve();
      });

      const requestedUrls = mockFetch.fn.mock.calls.map(([input]) =>
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url,
      );
      expect(
        requestedUrls.some((url) =>
          url.includes("/api/chat/claude-ext-789/messages?after_seq=5"),
        ),
      ).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("defaults activeAgent to default and resets to default on a fresh chat", async () => {
    localStorage.clear();

    await loadModule();
    const { result } = renderHook(() => useChat());

    expect(result.current.activeAgent).toBe("default");

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "session_info",
        session_ref: "#42",
        agent_name: "developer",
      });
    });

    expect(result.current.activeAgent).toBe("developer");

    act(() => result.current.startNewChat());

    expect(result.current.activeAgent).toBe("default");
  });

  it("sendMessage adds user message and sends WS message", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.sendMessage("Hello world");
    });

    // Should have added a user message
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("user");
    expect(result.current.messages[0].content).toBe("Hello world");

    // Should be streaming
    expect(result.current.isStreaming).toBe(true);
  });

  it("sendMessage includes the selected provider after switching state", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.setSelectedProvider("gemini");
    });

    act(() => {
      result.current.sendMessage("Hello through Gemini");
    });

    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(sentMsg.type).toBe("chat_message");
    expect(sentMsg.provider).toBe("gemini");
  });

  it("continueSessionInChat preserves the source session provider when resuming", async () => {
    mockFetch.mockJsonResponse("/api/sessions/source-session", {
      session: {
        id: "source-session",
        project_id: "proj-source",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        context_window: null,
      },
    });
    mockFetch.mockJsonResponse(
      "/api/sessions/source-session/messages?limit=100",
      {
        messages: [],
      },
    );

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.setSelectedProvider("gemini");
    });

    await act(async () => {
      await result.current.continueSessionInChat(
        "source-session",
        "proj-source",
      );
    });

    expect(result.current.dbSessionId).toBe("source-session");
    expect(result.current.conversationId).toBe("source-session");
    expect(result.current.selectedProvider).toBe("codex");
    expect(result.current.isContinuingSession).toBe(true);

    const continueMsg = ws.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .find((msg) => msg.type === "continue_in_chat");
    expect(continueMsg?.conversation_id).toBe("source-session");
    expect(continueMsg?.source_session_id).toBe("source-session");
    expect(continueMsg?.provider).toBe("codex");
    expect(continueMsg?.model).toBe("gpt-5.4");
  });

  it("continueSessionInChat forwards fallback_context when requested", async () => {
    mockFetch.mockJsonResponse("/api/sessions/source-session", {
      session: {
        id: "source-session",
        project_id: "proj-source",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        context_window: null,
      },
    });
    mockFetch.mockJsonResponse(
      "/api/sessions/source-session/messages?limit=100",
      {
        messages: [],
      },
    );

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      await result.current.continueSessionInChat("source-session", "proj-source", {
        fallbackContext: "auto",
      });
    });

    const continueMsg = ws.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .find((msg) => msg.type === "continue_in_chat");
    expect(continueMsg?.fallback_context).toBe("auto");
  });

  it("switchProvider creates a new server-owned session with the requested provider for an existing chat", async () => {
    // After the session identity unification, switchProvider no longer sends
    // set_provider/set_agent via WebSocket. It calls ensureMainSession with
    // forceNew=true and the provider, which POSTs to /api/sessions/web-chat.
    // The backend persists the provider on the new session.
    mockFetch.mockJsonResponse("/api/sessions/web-chat", {
      session: {
        id: "new-codex-session",
        source: "codex",
        model: null,
        chat_mode: null,
        seq_num: 42,
        title: null,
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.sendMessage("Existing message");
    });

    act(() => {
      result.current.switchProvider("codex");
    });

    // REST call should include the provider in its request body
    const sessionCalls = mockFetch.fn.mock.calls.filter(
      ([url]) =>
        typeof url === "string" && url.includes("/api/sessions/web-chat"),
    );
    expect(sessionCalls.length).toBeGreaterThan(0);
    const body = JSON.parse(
      sessionCalls[sessionCalls.length - 1][1].body as string,
    );
    expect(body.provider).toBe("codex");
  });

  it("switchConversation syncs the selected provider from the restored web chat session", async () => {
    mockFetch.mockJsonResponse(
      "/api/chat/db-session-2/messages?limit=100&after_seq=0",
      {
        messages: [],
        max_seq: 5,
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/db-session-2", {
      session: {
        id: "db-session-2",
        seq_num: 202,
        title: "Other chat",
        source: "codex",
        session_type: "web_chat",
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        context_window: null,
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.setSelectedProvider("gemini");
    });

    await act(async () => {
      result.current.switchConversation("db-session-2");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.selectedProvider).toBe("codex");
  });

  it("sendMessage queues message when WS not connected", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    // Don't open WS — message should be queued (returns true)
    let sent: boolean = false;
    act(() => {
      sent = result.current.sendMessage("Hello");
    });

    expect(sent).toBe(true);
    // Message should appear in UI even though WS is disconnected
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("Hello");
    expect(result.current.messages[0].role).toBe("user");
  });

  it("startNewChat clears messages without creating a session", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    expect(result.current.messages).toHaveLength(1);

    const oldId = result.current.conversationId;

    act(() => result.current.startNewChat());

    expect(result.current.messages).toHaveLength(0);
    expect(result.current.conversationId).not.toBe(oldId);
    expect(result.current.dbSessionId).toBeNull();

    const sessionCalls = mockFetch.fn.mock.calls.filter(
      ([url]) =>
        typeof url === "string" && url.includes("/api/sessions/web-chat"),
    );
    expect(sessionCalls).toHaveLength(0);
  });

  it("startNewChat clears viewed-session chrome and queued proxy notices", async () => {
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-proxy/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-proxy", {
      session: {
        id: "sess-proxy",
        seq_num: 2315,
        source: "claude",
        title: "Proxy session",
        status: "active",
        model: "sonnet",
        external_id: "proxy-ext",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "terminal",
      },
    });
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-proxy");
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      result.current.observeSession?.("sess-proxy", "proxy");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-proxy",
        external_id: "proxy-ext",
        source: "claude",
        title: "Proxy session",
        status: "active",
        model: "sonnet",
        ref: "#2315",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
      ws.simulateMessage({
        type: "send_to_cli_session_result",
        session_id: "sess-proxy",
        delivered: false,
        delivery_method: "hook_piggyback",
      });
    });

    expect(result.current.viewingSessionId).toBe("sess-proxy");
    expect(result.current.proxyDeliveryNotice).toBe(
      "Message queued until the session yields.",
    );

    act(() => result.current.startNewChat());

    expect(result.current.viewingSessionId).toBeNull();
    expect(result.current.viewingSessionMeta).toBeNull();
    expect(result.current.attachedSessionId).toBeNull();
    expect(result.current.sessionInteractionMode).toBe("none");
    expect(result.current.proxyDeliveryNotice).toBeNull();
    expect(result.current.dbSessionId).toBeNull();
  });

  it("switchProvider keeps a blank draft local until first send", async () => {
    localStorage.clear();

    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.switchProvider("codex");
    });

    expect(result.current.selectedProvider).toBe("codex");
    expect(result.current.dbSessionId).toBeNull();

    const sessionCalls = mockFetch.fn.mock.calls.filter(
      ([url]) =>
        typeof url === "string" && url.includes("/api/sessions/web-chat"),
    );
    expect(sessionCalls).toHaveLength(0);
  });

  it("hydrates resumed main-session metadata from session_continued", async () => {
    mockFetch.mockJsonResponse("/api/sessions/test-conversation-id", {
      session: {
        id: "test-conversation-id",
        source: "claude",
        session_type: "web_chat",
        status: "active",
      },
    });
    mockFetch.mockJsonResponse("/api/sessions/db-session-continued", {
      session: {
        id: "db-session-continued",
        seq_num: 88,
        title: "Continued Session",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        status: "active",
        usage_input_tokens: 320,
        usage_output_tokens: 40,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 50,
        context_window: 200000,
        session_type: "web_chat",
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const modeChanged = vi.fn();
    act(() => result.current.setOnModeChanged(modeChanged));

    act(() => {
      ws.simulateMessage({
        type: "session_continued",
        conversation_id: result.current.conversationId,
        db_session_id: "db-session-continued",
        source_session_id: "source-session",
        ref: "#88",
        title: "Continued Session",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        status: "active",
        session_type: "web_chat",
      });
    });

    await waitFor(() => {
      expect(result.current.dbSessionId).toBe("db-session-continued");
      expect(result.current.sessionRef).toBe("#88");
      expect(result.current.sessionTitle).toBe("Continued Session");
      expect(result.current.selectedProvider).toBe("codex");
      expect(result.current.mainSessionMeta?.title).toBe("Continued Session");
      expect(result.current.mainSessionMeta?.model).toBe("gpt-5.4");
      expect(result.current.contextUsage.totalInputTokens).toBe(320);
      expect(result.current.contextUsage.contextWindow).toBe(200000);
    });

    expect(modeChanged).toHaveBeenCalledWith("normal");
  });

  it("restores the previous chat state when continueSessionInChat fails", async () => {
    mockFetch.mockJsonResponse(
      "/api/sessions/source-session/messages?limit=100",
      {
        messages: [
          {
            id: "source-msg-1",
            role: "assistant",
            content: "Source output",
            timestamp: "2026-04-14T00:00:00Z",
          },
        ],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/source-session", {
      session: {
        id: "source-session",
        project_id: "proj-source",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        usage_input_tokens: 120,
        usage_output_tokens: 20,
        usage_cache_read_tokens: 50,
        usage_cache_creation_tokens: 10,
        context_window: 200000,
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.setSelectedProvider("gemini");
      result.current.sendMessage("Existing draft");
    });

    await act(async () => {
      await result.current.continueSessionInChat(
        "source-session",
        "proj-source",
      );
    });

    await waitFor(() => {
      expect(result.current.selectedProvider).toBe("codex");
      expect(result.current.messages[0]?.content).toBe("Source output");
    });

    act(() => {
      ws.simulateMessage({
        type: "error",
        message: "Continuation failed",
      });
    });

    await waitFor(() => {
      expect(result.current.conversationId).toBe("test-conversation-id");
      expect(result.current.dbSessionId).toBe("test-conversation-id");
      expect(result.current.selectedProvider).toBe("gemini");
      expect(result.current.messages[0].content).toBe("Existing draft");
    });

    expect(
      result.current.messages.some(
        (message) =>
          message.role === "system" &&
          message.content === "Continuation failed",
      ),
    ).toBe(true);
  });

  it("ignores stale continuation errors after session_continued succeeds", async () => {
    mockFetch.mockJsonResponse("/api/sessions/source-session", {
      session: {
        id: "source-session",
        project_id: "proj-source",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        context_window: null,
      },
    });
    mockFetch.mockJsonResponse(
      "/api/sessions/source-session/messages?limit=100",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/db-session-continued", {
      session: {
        id: "db-session-continued",
        seq_num: 88,
        title: "Continued Session",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        status: "active",
        usage_input_tokens: 320,
        usage_output_tokens: 40,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 50,
        context_window: 200000,
      },
    });

    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      await result.current.continueSessionInChat(
        "source-session",
        "proj-source",
      );
    });

    act(() => {
      ws.simulateMessage({
        type: "session_continued",
        conversation_id: result.current.conversationId,
        db_session_id: "db-session-continued",
        source_session_id: "source-session",
        ref: "#88",
        title: "Continued Session",
        source: "codex",
        model: "gpt-5.4",
        chat_mode: "accept_edits",
        status: "active",
        session_type: "web_chat",
      });
    });

    await waitFor(() => {
      expect(result.current.dbSessionId).toBe("db-session-continued");
      expect(result.current.selectedProvider).toBe("codex");
    });

    act(() => {
      ws.simulateMessage({
        type: "error",
        message: "stale continuation error",
      });
    });

    expect(result.current.dbSessionId).toBe("db-session-continued");
    expect(result.current.selectedProvider).toBe("codex");
  });
});
