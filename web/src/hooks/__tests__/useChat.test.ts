/**
 * Tests for useChat hook — focuses on pure helper functions and key behaviors.
 * The hook is ~2000 lines with complex WS state management. We test:
 * 1. Pure functions: mapApiMessages, appendTextBlock, appendToolBlock, findPendingToolCall, uuid
 * 2. Key hook behaviors: conversation ID management, message state
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  createMockWebSocket,
  type MockWebSocketInstance,
} from "../../test/mocks/websocket";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../test/mocks/fetch";

let mockWs: {
  instances: MockWebSocketInstance[];
  MockWebSocket: typeof WebSocket;
  restore: () => void;
};
let mockFetch: MockFetchInstance;
let useChat: typeof import("../useChat").useChat;
let originalLocalStorage: Storage;
let consoleSpy: {
  log: ReturnType<typeof vi.spyOn>;
  error: ReturnType<typeof vi.spyOn>;
  warn: ReturnType<typeof vi.spyOn>;
};

beforeEach(() => {
  consoleSpy = {
    log: vi.spyOn(console, "log").mockImplementation(() => {}),
    error: vi.spyOn(console, "error").mockImplementation(() => {}),
    warn: vi.spyOn(console, "warn").mockImplementation(() => {}),
  };
  mockWs = createMockWebSocket();
  mockFetch = createMockFetch();
  // Mock localStorage — jsdom's localStorage doesn't delegate to Storage.prototype,
  // so vi.spyOn(Storage.prototype, ...) won't intercept calls. Replace the object directly.
  // Seed a conversation id so useChat initializes with a bound session on mount.
  // After the session identity unification (cb80f0462), loadConversationId() no
  // longer auto-generates a uuid — it returns "" unless localStorage has one,
  // and a real main session is created lazily via ensureMainSession/REST. Tests
  // can't hit that REST endpoint, so we pre-bind here to match runtime state.
  originalLocalStorage = globalThis.localStorage;
  const store: Record<string, string> = {
    "gobby-conversation-id": "test-conversation-id",
    "gobby-db-session-id": "test-conversation-id",
  };
  const mockStorage = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      Object.keys(store).forEach((k) => delete store[k]);
    }),
    key: vi.fn((_index: number) => null),
    get length() {
      return Object.keys(store).length;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: mockStorage,
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  mockWs.restore();
  mockFetch.restore();
  Object.defineProperty(globalThis, "localStorage", {
    value: originalLocalStorage,
    writable: true,
    configurable: true,
  });
  consoleSpy.log.mockRestore();
  consoleSpy.error.mockRestore();
  consoleSpy.warn.mockRestore();
  vi.restoreAllMocks();
});

async function loadModule() {
  vi.resetModules();
  const mod = await import("../useChat");
  useChat = mod.useChat;
}

describe("useChat", () => {
  it("initializes with empty messages and not streaming", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    expect(result.current.messages).toEqual([]);
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.isThinking).toBe(false);
    expect(result.current.isConnected).toBe(false);
  });

  it("connects to WebSocket on mount", async () => {
    await loadModule();
    renderHook(() => useChat());

    expect(mockWs.instances).toHaveLength(1);
    expect(mockWs.instances[0].url).toContain("/ws");
  });

  it("restores persisted chat messages as string content on mount", async () => {
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

  it("sets isConnected when WS opens", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    act(() => mockWs.instances[0].simulateOpen());

    expect(result.current.isConnected).toBe(true);
  });

  it("sends subscribe message on connect", async () => {
    await loadModule();
    renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    expect(ws.send).toHaveBeenCalled();
    const msg = JSON.parse(ws.send.mock.calls[0][0]);
    expect(msg.type).toBe("subscribe");
    expect(msg.events).toContain("chat_stream");
    expect(msg.events).toContain("tool_status");
    expect(msg.events).toContain("session_message");
  });

  it("resets state on WS close", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    expect(result.current.isConnected).toBe(true);

    act(() => ws.simulateClose());
    expect(result.current.isConnected).toBe(false);
    expect(result.current.isStreaming).toBe(false);
  });

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

  it("restores a watched session on mount without hydrating the parked main chat over it", async () => {
    localStorage.setItem("gobby-viewing-session-id", "sess-view");
    localStorage.setItem("gobby-viewing-session-mode", "observe");

    mockFetch.mockJsonResponse(
      "/api/sessions/sess-view/messages?limit=100&offset=0",
      {
        messages: [
          {
            id: "sess-msg-1",
            role: "assistant",
            content: "Watched output",
            timestamp: "2026-04-14T00:00:00Z",
            content_blocks: [{ type: "text", content: "Watched output" }],
          },
        ],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-view", {
      session: {
        id: "sess-view",
        seq_num: 321,
        source: "claude",
        title: "Watched Terminal",
        status: "active",
        model: "sonnet",
        external_id: "sess-view-ext",
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

    await waitFor(() => {
      expect(result.current.viewingSessionId).toBe("sess-view");
      expect(result.current.messages[0].content).toBe("Watched output");
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
        url.includes("/api/chat/test-conversation-id/messages"),
      ),
    ).toBe(false);

    const attachMsg = ws.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .find((msg) => msg.type === "attach_to_session");
    expect(attachMsg?.session_id).toBe("sess-view");
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

  it("handles chat_stream messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    // Send a user message first to establish a request ID
    act(() => result.current.sendMessage("Hello"));

    // Get the request_id from the sent WS message
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    const requestId = sentMsg.request_id;

    // Simulate streaming response
    act(() => {
      ws.simulateMessage({
        type: "chat_stream",
        message_id: "msg-1",
        request_id: requestId,
        content: "Hello ",
        done: false,
      });
    });

    // Should have an assistant message
    const assistantMsgs = result.current.messages.filter(
      (m) => m.role === "assistant",
    );
    expect(assistantMsgs).toHaveLength(1);
    expect(assistantMsgs[0].content).toContain("Hello");
  });

  it("handles chat_stream done=true", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    const requestId = sentMsg.request_id;

    act(() => {
      ws.simulateMessage({
        type: "chat_stream",
        message_id: "msg-1",
        request_id: requestId,
        content: "Response",
        done: true,
      });
    });

    expect(result.current.isStreaming).toBe(false);
  });

  it("handles chat_error messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    const requestId = sentMsg.request_id;

    act(() => {
      ws.simulateMessage({
        type: "chat_error",
        message_id: "msg-1",
        request_id: requestId,
        error: "Something went wrong",
      });
    });

    expect(result.current.isStreaming).toBe(false);
    // Error should appear in messages
    const errorMsgs = result.current.messages.filter(
      (m) => m.role === "system",
    );
    expect(errorMsgs.length).toBeGreaterThanOrEqual(1);
  });

  it("handles tool_status messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    const requestId = sentMsg.request_id;

    // First stream some text
    act(() => {
      ws.simulateMessage({
        type: "chat_stream",
        message_id: "msg-1",
        request_id: requestId,
        content: "",
        done: false,
      });
    });

    // Then a tool status
    act(() => {
      ws.simulateMessage({
        type: "tool_status",
        message_id: "msg-1",
        request_id: requestId,
        tool_call_id: "tc-1",
        status: "calling",
        tool_name: "read_file",
        server_name: "gobby",
        arguments: { path: "/tmp/test" },
      });
    });

    const assistantMsgs = result.current.messages.filter(
      (m) => m.role === "assistant",
    );
    expect(assistantMsgs).toHaveLength(1);
    // The message should have tool calls
    const msg = assistantMsgs[0];
    expect(msg.toolCalls?.length).toBeGreaterThanOrEqual(1);
    expect(msg.toolCalls?.[0].tool_name).toBe("read_file");
    expect(msg.toolCalls?.[0].tool_type).toBe("read");
  });

  it("stopStreaming stops streaming", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    expect(result.current.isStreaming).toBe(true);

    act(() => result.current.stopStreaming());
    expect(result.current.isStreaming).toBe(false);
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

  it("handles voice_transcription messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "voice_transcription",
        text: "Hello from voice",
        request_id: "voice-req-1",
      });
    });

    // Should add a user message from voice
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("user");
    expect(result.current.messages[0].content).toBe("Hello from voice");
    expect(result.current.isStreaming).toBe(true);
  });

  it("handles session_info messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "session_info",
        session_ref: "#42",
        current_branch: "feature/test",
        agent_name: "test-agent",
      });
    });

    expect(result.current.sessionRef).toBe("#42");
    expect(result.current.currentBranch).toBe("feature/test");
    expect(result.current.activeAgent).toBe("test-agent");
  });

  it("upserts rendered session_message events while viewing a session", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-1/messages?limit=100&offset=0",
      {
        messages: [
          {
            id: "sess-msg-1",
            role: "assistant",
            content: "Initial output",
            timestamp: "2026-04-09T00:00:00Z",
            content_blocks: [{ type: "text", content: "Initial output" }],
          },
        ],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-1", {
      session: {
        id: "sess-1",
        seq_num: 2310,
        source: "codex",
        title: "Observed session",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-1",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
      },
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-1");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("Initial output");

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-1",
        message: {
          id: "sess-msg-1",
          role: "assistant",
          content: "Updated output",
          timestamp: "2026-04-09T00:00:01Z",
          content_blocks: [{ type: "text", content: "Updated output" }],
        },
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].content).toBe("Updated output");
  });

  it("reclassifies protocol-tagged session_message events as system while viewing a session", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-1/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-1", {
      session: {
        id: "sess-1",
        seq_num: 2310,
        source: "codex",
        title: "Observed session",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-1",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
      },
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-1");
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-1",
        message: {
          id: "sess-protocol-1",
          role: "user",
          content:
            '<local-command-stdout><stdout>npm test</stdout></local-command-stdout>',
          timestamp: "2026-04-09T00:00:01Z",
        },
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("system");
  });

  it("reclassifies rendered protocol-only session_message events as system while viewing a session", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-1/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-1", {
      session: {
        id: "sess-1",
        seq_num: 2310,
        source: "codex",
        title: "Observed session",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-1",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
      },
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-1");
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-1",
        message: {
          id: "sess-protocol-rendered-1",
          role: "user",
          content: "",
          timestamp: "2026-04-09T00:00:01Z",
          content_blocks: [
            {
              type: "tool_chain",
              tool_calls: [
                {
                  id: "protocol-1",
                  tool_name: "protocol_context",
                  server_name: "builtin",
                  tool_type: "protocol",
                  status: "completed",
                },
              ],
            },
          ],
        },
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].role).toBe("system");
  });

  it("attachToViewed upgrades an active watched terminal session into proxy mode", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-view/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-view", {
      session: {
        id: "sess-view",
        seq_num: 2310,
        source: "claude",
        title: "Viewed Terminal",
        status: "active",
        model: "sonnet",
        external_id: "claude-ext-view",
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

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-view");
      await Promise.resolve();
      await Promise.resolve();
    });

    const sendCountBeforeAttach = ws.send.mock.calls.length;
    act(() => {
      result.current.attachToViewed?.();
    });

    const attachMsg = JSON.parse(ws.send.mock.calls[sendCountBeforeAttach][0]);
    expect(attachMsg).toMatchObject({
      type: "attach_to_session",
      session_id: "sess-view",
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-view",
        external_id: "claude-ext-view",
        source: "claude",
        title: "Viewed Terminal",
        status: "active",
        model: "sonnet",
        ref: "#2310",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.sessionInteractionMode).toBe("proxy");
    expect(result.current.attachedSessionId).toBe("sess-view");
  });

  it.each([
    ["codex", "gpt-5.4"],
    ["gemini", "gemini-2.5-pro"],
    ["qwen", "qwen3-coder"],
  ])(
    "keeps live %s tmux sessions attachable even when the session row is handoff_ready",
    async (source, model) => {
      await loadModule();
      mockFetch.mockJsonResponse(
        "/api/sessions/sess-live-handoff/messages?limit=100&offset=0",
        {
          messages: [],
        },
      );
      mockFetch.mockJsonResponse("/api/sessions/sess-live-handoff", {
        session: {
          id: "sess-live-handoff",
          seq_num: 2310,
          source,
          title: "Live handoff terminal",
          status: "handoff_ready",
          can_proxy_attach: true,
          model,
          external_id: `${source}-ext-view`,
          chat_mode: "accept_edits",
          git_branch: "main",
          context_window: 200000,
          usage_input_tokens: 0,
          usage_output_tokens: 0,
          usage_cache_read_tokens: 0,
          usage_cache_creation_tokens: 0,
          session_type: "terminal",
          terminal_context: { tmux_pane: "%18" },
        },
      });

      const { result } = renderHook(() => useChat());
      const ws = mockWs.instances[0];
      act(() => ws.simulateOpen());

      await act(async () => {
        result.current.viewSession("sess-live-handoff");
        await Promise.resolve();
        await Promise.resolve();
      });

      act(() => {
        result.current.attachToViewed?.();
      });

      act(() => {
        ws.simulateMessage({
          type: "attach_to_session_result",
          session_id: "sess-live-handoff",
          external_id: `${source}-ext-view`,
          source,
          title: "Live handoff terminal",
          status: "handoff_ready",
          can_proxy_attach: true,
          model,
          ref: "#2310",
          chat_mode: "accept_edits",
          git_branch: "main",
          context_window: 200000,
          session_type: "terminal",
          messages: [],
          total_count: 0,
        });
      });

      expect(result.current.viewingSessionMeta?.canProxyAttach).toBe(true);
      expect(result.current.sessionInteractionMode).toBe("proxy");
      expect(result.current.attachedSessionId).toBe("sess-live-handoff");
    },
  );

  it("keeps autonomous session observation read-only", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-auto/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-auto/messages?limit=100", {
      messages: [],
    });
    mockFetch.mockJsonResponse("/api/sessions/sess-auto", {
      session: {
        id: "sess-auto",
        seq_num: 2311,
        source: "claude",
        title: "Autonomous session",
        status: "active",
        model: "sonnet",
        external_id: "claude-ext-auto",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "terminal",
        workflow_name: "release-checks",
        agent_run_id: "run-auto-1",
      },
    });
    mockFetch.mockJsonResponse("/api/agents/runs/run-auto-1", {
      run: { agent_name: "code-reviewer", workflow_name: "release-checks" },
    });
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-auto");
      result.current.observeSession?.("sess-auto", "observe");
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-auto",
        external_id: "claude-ext-auto",
        source: "claude",
        title: "Autonomous session",
        status: "active",
        model: "sonnet",
        ref: "#2311",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        session_type: "terminal",
        workflow_name: "release-checks",
        agent_run_id: "run-auto-1",
        agent_name: "code-reviewer",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.sessionInteractionMode).toBe("observe");
    expect(result.current.attachedSessionId).toBeNull();
    expect(result.current.viewingSessionMeta?.agentRunId).toBe("run-auto-1");

    const sendCountBeforeAttach = ws.send.mock.calls.length;
    act(() => {
      result.current.attachToViewed?.();
    });

    expect(result.current.sessionInteractionMode).toBe("observe");
    expect(result.current.attachedSessionId).toBeNull();
    expect(result.current.viewingSessionId).toBe("sess-auto");
    expect(ws.send.mock.calls).toHaveLength(sendCountBeforeAttach);
  });

  it("does not attach viewed web chat sessions into proxy mode", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-web/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-web", {
      session: {
        id: "sess-web",
        seq_num: 2313,
        source: "claude",
        title: "Other Web Chat",
        status: "paused",
        model: "sonnet",
        external_id: "claude-ext-web",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "web_chat",
      },
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-web");
      await Promise.resolve();
      await Promise.resolve();
    });

    const sendCountBeforeAttach = ws.send.mock.calls.length;
    act(() => {
      result.current.attachToViewed?.();
    });

    expect(result.current.attachedSessionId).toBeNull();
    expect(result.current.sessionInteractionMode).toBe("none");
    expect(ws.send.mock.calls).toHaveLength(sendCountBeforeAttach);
  });

  it("does not attach viewed sessions with unknown session types", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-unknown/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-unknown", {
      session: {
        id: "sess-unknown",
        seq_num: 2314,
        source: "claude",
        title: "Unknown Session Type",
        status: "active",
        model: "sonnet",
        external_id: "claude-ext-unknown",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "mystery_mode",
      },
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-unknown");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.viewingSessionMeta?.sessionType).toBeNull();

    const sendCountBeforeAttach = ws.send.mock.calls.length;
    act(() => {
      result.current.attachToViewed?.();
    });

    expect(ws.send.mock.calls).toHaveLength(sendCountBeforeAttach);
  });

  it("keeps chat_message routing even if a web chat attach result is received", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-web",
        external_id: "web-ext",
        source: "claude",
        title: "Web Chat Session",
        status: "paused",
        model: "sonnet",
        ref: "#2314",
        session_type: "web_chat",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.attachedSessionId).toBeNull();
    expect(result.current.sessionInteractionMode).toBe("none");

    act(() => {
      result.current.sendMessage("Hello after swap");
    });

    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(sentMsg.type).toBe("chat_message");
    expect(sentMsg.content).toBe("Hello after swap");
  });

  it("shows a queued proxy notice when CLI delivery falls back to hook piggyback", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

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
        ref: "#2312",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    act(() => {
      result.current.sendMessage("/plan");
    });

    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(sentMsg.type).toBe("send_to_cli_session");
    expect(sentMsg.content).toBe("/plan");

    act(() => {
      ws.simulateMessage({
        type: "send_to_cli_session_result",
        session_id: "sess-proxy",
        delivered: false,
        delivery_method: "hook_piggyback",
      });
    });

    expect(result.current.proxyDeliveryNotice).toBe(
      "Message queued until the session yields.",
    );
  });

  it("restores attached terminal chat mode from attach metadata", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const modeChanged = vi.fn();
    act(() => result.current.setOnModeChanged(modeChanged));

    act(() => {
      result.current.observeSession?.("sess-proxy", "proxy");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-proxy",
        external_id: "proxy-ext",
        source: "codex",
        title: "Proxy session",
        status: "active",
        model: "gpt-5.4",
        reasoning_effort: "high",
        chat_mode: "accept_edits",
        ref: "#2312",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.sessionInteractionMode).toBe("proxy");
    expect(result.current.attachedSessionMeta?.reasoningEffort).toBe("high");
    expect(modeChanged).toHaveBeenCalledWith("normal");
  });

  it("reconciles optimistic proxy messages when the session broadcast arrives before the ack", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

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
        ref: "#2312",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    act(() => {
      result.current.sendMessage("hello world");
    });

    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(sentMsg.type).toBe("send_to_cli_session");
    expect(sentMsg.client_message_id).toEqual(expect.any(String));
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe(`user-${sentMsg.client_message_id}`);

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-proxy",
        message: {
          id: "db-msg-1",
          role: "user",
          content: "hello world",
          timestamp: "2026-04-17T20:00:00Z",
        },
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("db-msg-1");
    expect(result.current.messages[0].content).toBe("hello world");

    act(() => {
      ws.simulateMessage({
        type: "send_to_cli_session_result",
        session_id: "sess-proxy",
        delivered: true,
        delivery_method: "tmux",
        message_id: "db-msg-1",
        client_message_id: sentMsg.client_message_id,
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("db-msg-1");
  });

  it("reconciles duplicate proxy messages by FIFO session order instead of matching on content", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

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
        ref: "#2312",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    act(() => {
      result.current.sendMessage("repeat me");
      result.current.sendMessage("repeat me");
    });

    expect(result.current.messages).toHaveLength(2);
    const firstPendingId = result.current.messages[0].id;
    const secondPendingId = result.current.messages[1].id;

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-proxy",
        message: {
          id: "db-msg-1",
          role: "user",
          content: "repeat me",
          timestamp: "2026-04-17T20:00:00Z",
        },
      });
    });

    expect(result.current.messages[0].id).toBe("db-msg-1");
    expect(result.current.messages[1].id).toBe(secondPendingId);
    expect(result.current.messages[1].id).not.toBe(firstPendingId);
  });

  it("keeps paused terminal sessions view-only instead of enabling proxy send mode", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.observeSession?.("sess-paused", "proxy");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-paused",
        external_id: "paused-ext",
        source: "codex",
        title: "Paused terminal",
        status: "paused",
        model: "gpt-5.4",
        ref: "#2313",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.sessionInteractionMode).toBe("none");
    expect(result.current.proxyDeliveryNotice).toBe(
      "This terminal session is paused. Use Resume Session to continue it in web chat.",
    );
  });

  it("shows a resume-only notice for non-attachable handoff terminals", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.observeSession?.("sess-handoff", "proxy");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-handoff",
        external_id: "handoff-ext",
        source: "codex",
        title: "Resume-only terminal",
        status: "handoff_ready",
        can_proxy_attach: false,
        model: "gpt-5.4",
        ref: "#2314",
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.sessionInteractionMode).toBe("none");
    expect(result.current.proxyDeliveryNotice).toBe(
      "This terminal session can only be resumed in web chat right now.",
    );
  });

  it("handles mode_changed messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const modeChanged = vi.fn();
    act(() => result.current.setOnModeChanged(modeChanged));

    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "bypass",
        conversation_id: result.current.conversationId,
      });
    });

    expect(modeChanged).toHaveBeenCalledWith("bypass");
  });

  it("hydrates resumed main-session metadata from session_continued", async () => {
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

  it("handles plan_pending_approval messages", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        plan_content: "# My Plan\n\nStep 1...",
      });
    });

    expect(result.current.planPendingApproval).toBe(true);
  });

  it("contextUsage tracks token usage from chat_stream", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => result.current.sendMessage("Hello"));
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    const requestId = sentMsg.request_id;

    act(() => {
      ws.simulateMessage({
        type: "chat_stream",
        message_id: "msg-1",
        request_id: requestId,
        content: "Done",
        done: true,
        usage: {
          input_tokens: 100,
          output_tokens: 50,
          cache_read_input_tokens: 20,
          cache_creation_input_tokens: 10,
          total_input_tokens: 130,
        },
        context_window: 200000,
      });
    });

    expect(result.current.contextUsage.totalInputTokens).toBeGreaterThan(0);
    expect(result.current.contextUsage.contextWindow).toBe(200000);
  });

  it("hydrates context usage from attach_to_session_result without changing the main chat id", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.observeSession?.("sess-proxy", "proxy");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-proxy",
        external_id: "proxy-ext",
        source: "codex",
        title: "Proxy Session",
        status: "active",
        model: "gpt-5.4",
        ref: "#300",
        session_type: "terminal",
        usage_input_tokens: 250,
        usage_output_tokens: 30,
        usage_cache_read_tokens: 90,
        usage_cache_creation_tokens: 20,
        context_window: 200000,
        messages: [],
        total_count: 0,
      });
    });

    expect(result.current.dbSessionId).toBe("test-conversation-id");
    expect(result.current.viewingSessionId).toBe("sess-proxy");
    expect(result.current.attachedSessionId).toBe("sess-proxy");
    expect(result.current.contextUsage).toMatchObject({
      totalInputTokens: 250,
      outputTokens: 30,
      cacheReadTokens: 90,
      cacheCreationTokens: 20,
      uncachedInputTokens: 140,
      contextWindow: 200000,
    });
  });

  it("sends set_project message on connect if projectIdRef is set", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    act(() => {
      result.current.setProjectIdRef("test-project-123");
    });

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const calls = ws.send.mock.calls.map((c) => JSON.parse(c[0]));
    const projectMsg = calls.find((m) => m.type === "set_project");

    expect(projectMsg).toBeDefined();
    expect(projectMsg.project_id).toBe("test-project-123");
  });

  it("sendProjectChange updates ref and sends WS message", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    ws.send.mockClear();

    act(() => {
      result.current.sendProjectChange("new-project-456");
    });

    const calls = ws.send.mock.calls.map((c) => JSON.parse(c[0]));
    const projectMsg = calls.find((m) => m.type === "set_project");
    expect(projectMsg).toBeDefined();
    expect(projectMsg.project_id).toBe("new-project-456");
  });
});
