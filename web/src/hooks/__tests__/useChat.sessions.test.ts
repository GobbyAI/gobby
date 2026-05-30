import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  cleanupUseChatTestContext,
  createUseChatTestContext,
  loadUseChatModule,
  type UseChatTestContext,
} from "./useChat.setup";
import { createWebChatSession } from "../useChat/sessionRecords";

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

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" },
  });
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.toString()
      : input.url;
}

describe("useChat viewed session state", () => {
  it("rejects invalid created web-chat session responses", async () => {
    mockFetch.mockJsonResponse("/api/sessions/web-chat", {
      session: {
        id: 123,
        source: "claude",
        model: null,
        chat_mode: "plan",
        seq_num: null,
        title: null,
      },
    });

    await expect(createWebChatSession()).rejects.toThrow(
      "Invalid web chat session response",
    );
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

  it("restores a proxy-attached terminal session on mount", async () => {
    localStorage.setItem("gobby-viewing-session-id", "sess-proxy-restore");
    localStorage.setItem("gobby-viewing-session-mode", "proxy");

    mockFetch.mockJsonResponse(
      "/api/sessions/sess-proxy-restore/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-proxy-restore", {
      session: {
        id: "sess-proxy-restore",
        seq_num: 322,
        source: "codex",
        title: "Attached Terminal",
        status: "active",
        model: "gpt-5.4",
        external_id: "sess-proxy-ext",
        chat_mode: "bypass",
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
      const attachMsg = ws.send.mock.calls
        .map(([raw]) => JSON.parse(raw))
        .find((msg) => msg.type === "attach_to_session");
      expect(attachMsg?.session_id).toBe("sess-proxy-restore");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-proxy-restore",
        external_id: "sess-proxy-ext",
        source: "codex",
        title: "Attached Terminal",
        status: "active",
        can_proxy_attach: true,
        model: "gpt-5.4",
        ref: "#322",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    await waitFor(() => {
      expect(result.current.viewingSessionId).toBe("sess-proxy-restore");
      expect(result.current.attachedSessionId).toBe("sess-proxy-restore");
      expect(result.current.sessionInteractionMode).toBe("proxy");
      expect(localStorage.getItem("gobby-viewing-session-mode")).toBe("proxy");
    });
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

  it("keeps zero session sequence numbers in viewed metadata refs", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-zero/messages?limit=100&offset=0",
      { messages: [] },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-zero", {
      session: {
        id: "sess-zero",
        seq_num: 0,
        source: "codex",
        title: "Zero sequence",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-zero",
        chat_mode: "bypass",
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
    act(() => mockWs.instances[0].simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-zero");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.viewingSessionMeta?.ref).toBe("#0");
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

    localStorage.setItem("gobby-fresh-chat-draft", "1");
    await act(async () => {
      result.current.viewSession("sess-view");
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(localStorage.getItem("gobby-fresh-chat-draft")).toBeNull();

    const sendCountBeforeAttach = ws.send.mock.calls.length;
    localStorage.setItem("gobby-fresh-chat-draft", "1");
    act(() => {
      result.current.attachToViewed?.();
    });
    expect(localStorage.getItem("gobby-fresh-chat-draft")).toBeNull();

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

  it("force-refreshes an already observed session when swapping it back", async () => {
    await loadModule();
    let messageFetchCount = 0;
    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.includes("/api/sessions/sess-swap/messages?limit=100&offset=0")) {
        messageFetchCount += 1;
        return jsonResponse({
          messages:
            messageFetchCount === 1
              ? [
                  {
                    id: "summary-msg",
                    role: "assistant",
                    content: "Summary pane stale content",
                    timestamp: "2026-04-09T00:00:00Z",
                    content_blocks: [
                      { type: "text", content: "Summary pane stale content" },
                    ],
                  },
                ]
              : [
                  {
                    id: "full-user",
                    role: "user",
                    content: "Run the checks",
                    timestamp: "2026-04-09T00:00:01Z",
                    content_blocks: [{ type: "text", content: "Run the checks" }],
                  },
                  {
                    id: "full-assistant",
                    role: "assistant",
                    content: "Full REST transcript",
                    timestamp: "2026-04-09T00:00:02Z",
                    content_blocks: [
                      { type: "text", content: "Full REST transcript" },
                    ],
                  },
                ],
        });
      }
      if (url.includes("/api/sessions/sess-swap")) {
        return jsonResponse({
          session: {
            id: "sess-swap",
            seq_num: 2312,
            source: "codex",
            title: "Swap Terminal",
            status: "active",
            model: "gpt-5.4",
            external_id: "codex-ext-swap",
            chat_mode: "bypass",
            git_branch: "main",
            context_window: 200000,
            usage_input_tokens: 0,
            usage_output_tokens: 0,
            usage_cache_read_tokens: 0,
            usage_cache_creation_tokens: 0,
            session_type: "terminal",
          },
        });
      }
      return jsonResponse({});
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-swap");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.messages.map((message) => message.content)).toEqual([
      "Summary pane stale content",
    ]);

    act(() => {
      result.current.observeSession?.("sess-swap", "observe");
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-swap",
        external_id: "codex-ext-swap",
        source: "codex",
        title: "Swap Terminal",
        status: "active",
        model: "gpt-5.4",
        ref: "#2312",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    await act(async () => {
      result.current.viewSession("sess-swap", { forceRefresh: true });
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.messages.map((message) => message.content)).toEqual([
        "Run the checks",
        "Full REST transcript",
      ]);
    });
    expect(messageFetchCount).toBe(2);
  });

  it("does not let clearViewingSession restore overwrite a later conversation switch", async () => {
    localStorage.setItem("gobby-conversation-id", "main-old");
    localStorage.setItem("gobby-db-session-id", "main-old");
    localStorage.setItem("gobby-viewing-session-id", "terminal-old");
    await loadModule();

    let resolveOldMessages: ((response: Response) => void) | null = null;
    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.includes("/api/sessions/terminal-old/messages?limit=100&offset=0")) {
        return jsonResponse({
          messages: [
            {
              id: "terminal-msg",
              role: "assistant",
              content: "Terminal transcript",
              timestamp: "2026-04-09T00:00:00Z",
              content_blocks: [{ type: "text", content: "Terminal transcript" }],
            },
          ],
        });
      }
      if (url.includes("/api/sessions/terminal-old")) {
        return jsonResponse({
          session: {
            id: "terminal-old",
            seq_num: 2412,
            source: "codex",
            title: "Old Terminal",
            status: "active",
            model: "gpt-5.4",
            external_id: "terminal-ext-old",
            session_type: "terminal",
          },
        });
      }
      if (url.includes("/api/chat/main-old/messages?limit=100&after_seq=0")) {
        return new Promise<Response>((resolve) => {
          resolveOldMessages = resolve;
        });
      }
      if (url.includes("/api/chat/main-new/messages?limit=100&after_seq=0")) {
        return jsonResponse({
          messages: [
            {
              id: "new-main-msg",
              role: "assistant",
              content: "New conversation transcript",
              timestamp: "2026-04-09T00:00:01Z",
            },
          ],
          max_seq: 7,
        });
      }
      if (url.includes("/api/sessions/main-old")) {
        return jsonResponse({
          session: { id: "main-old", source: "claude", chat_mode: "plan" },
        });
      }
      if (url.includes("/api/sessions/main-new")) {
        return jsonResponse({
          session: {
            id: "main-new",
            seq_num: 2501,
            source: "claude",
            title: "New Main",
            status: "active",
            model: "sonnet",
            external_id: "main-new-ext",
            session_type: "web_chat",
            chat_mode: "plan",
          },
        });
      }
      return jsonResponse({});
    });

    const { result } = renderHook(() => useChat());
    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    await waitFor(() => {
      expect(result.current.messages[0]?.content).toBe("Terminal transcript");
    });

    act(() => {
      result.current.clearViewingSession?.();
      result.current.switchConversation("main-new");
    });

    await waitFor(() => {
      expect(result.current.messages[0]?.content).toBe("New conversation transcript");
    });

    await act(async () => {
      resolveOldMessages?.(
        jsonResponse({
          messages: [
            {
              id: "old-main-msg",
              role: "assistant",
              content: "Old restored transcript",
              timestamp: "2026-04-09T00:00:02Z",
            },
          ],
          max_seq: 3,
        }),
      );
      await Promise.resolve();
    });

    expect(result.current.messages[0]?.content).toBe("New conversation transcript");
  });

  it("ignores stale detach acknowledgements after swapping watched terminal sessions", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-old/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-old", {
      session: {
        id: "sess-old",
        seq_num: 2310,
        source: "codex",
        title: "Old Terminal",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-old",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "terminal",
      },
    });
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-new/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-new", {
      session: {
        id: "sess-new",
        seq_num: 2311,
        source: "codex",
        title: "New Terminal",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-new",
        chat_mode: "bypass",
        git_branch: "feature/swap",
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
      result.current.viewSession("sess-old");
      await Promise.resolve();
      await Promise.resolve();
    });

    act(() => {
      result.current.observeSession?.("sess-old", "observe");
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-old",
        external_id: "codex-ext-old",
        source: "codex",
        title: "Old Terminal",
        status: "active",
        model: "gpt-5.4",
        ref: "#2310",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    const sendCountBeforeSwap = ws.send.mock.calls.length;

    await act(async () => {
      result.current.viewSession("sess-new");
      result.current.observeSession?.("sess-new", "observe");
      await Promise.resolve();
      await Promise.resolve();
    });

    const detachMsg = JSON.parse(ws.send.mock.calls[sendCountBeforeSwap][0]);
    const attachMsg = JSON.parse(ws.send.mock.calls[sendCountBeforeSwap + 1][0]);
    expect(detachMsg).toMatchObject({
      type: "detach_from_session",
      session_id: "sess-old",
    });
    expect(attachMsg).toMatchObject({
      type: "attach_to_session",
      session_id: "sess-new",
    });

    act(() => {
      ws.simulateMessage({
        type: "attach_to_session_result",
        session_id: "sess-new",
        external_id: "codex-ext-new",
        source: "codex",
        title: "New Terminal",
        status: "active",
        model: "gpt-5.4",
        ref: "#2311",
        chat_mode: "bypass",
        git_branch: "feature/swap",
        context_window: 200000,
        session_type: "terminal",
        messages: [],
        total_count: 0,
      });
    });

    act(() => {
      ws.simulateMessage({
        type: "detach_from_session_result",
        session_id: "sess-old",
      });
    });

    expect(result.current.viewingSessionId).toBe("sess-new");
    expect(result.current.viewingSessionMeta?.title).toBe("New Terminal");
    expect(result.current.sessionInteractionMode).toBe("observe");
    expect(result.current.attachedSessionId).toBeNull();
  });

  it("resets viewed context usage when switching to a zero-usage session", async () => {
    await loadModule();
    mockFetch.mockJsonResponse("/api/sessions/sess-old/messages?limit=100&offset=0", {
      messages: [],
    });
    mockFetch.mockJsonResponse("/api/sessions/sess-old", {
      session: {
        id: "sess-old",
        seq_num: 2410,
        source: "codex",
        title: "Old Terminal",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-old",
        chat_mode: "bypass",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 320,
        usage_output_tokens: 40,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 50,
        session_type: "terminal",
      },
    });
    mockFetch.mockJsonResponse("/api/sessions/sess-new/messages?limit=100&offset=0", {
      messages: [],
    });
    mockFetch.mockJsonResponse("/api/sessions/sess-new", {
      session: {
        id: "sess-new",
        seq_num: 2411,
        source: "codex",
        title: "New Terminal",
        status: "active",
        model: "gpt-5.4",
        external_id: "codex-ext-new",
        chat_mode: "bypass",
        git_branch: "feature/swap",
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
      result.current.viewSession("sess-old");
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.contextUsage).toMatchObject({
        totalInputTokens: 320,
        outputTokens: 40,
        cacheReadTokens: 120,
        cacheCreationTokens: 50,
        uncachedInputTokens: 150,
        contextWindow: 200000,
      });
    });

    await act(async () => {
      result.current.viewSession("sess-new");
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.contextUsage).toMatchObject({
        totalInputTokens: 0,
        outputTokens: 0,
        cacheReadTokens: 0,
        cacheCreationTokens: 0,
        uncachedInputTokens: 0,
        contextWindow: 200000,
      });
    });
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

  it("ignores non-string agent run names when resolving viewed session metadata", async () => {
    await loadModule();
    mockFetch.mockJsonResponse(
      "/api/sessions/sess-bad-agent/messages?limit=100&offset=0",
      {
        messages: [],
      },
    );
    mockFetch.mockJsonResponse("/api/sessions/sess-bad-agent", {
      session: {
        id: "sess-bad-agent",
        seq_num: 2312,
        source: "claude",
        title: "Bad agent name",
        status: "active",
        model: "sonnet",
        external_id: "claude-ext-bad-agent",
        chat_mode: "accept_edits",
        git_branch: "main",
        context_window: 200000,
        usage_input_tokens: 0,
        usage_output_tokens: 0,
        usage_cache_read_tokens: 0,
        usage_cache_creation_tokens: 0,
        session_type: "terminal",
        workflow_name: "release-checks",
        agent_run_id: "run-bad-agent",
      },
    });
    mockFetch.mockJsonResponse("/api/agents/runs/run-bad-agent", {
      run: { agent_name: { name: "not-a-string" }, workflow_name: 42 },
    });
    const { result } = renderHook(() => useChat());
    act(() => mockWs.instances[0].simulateOpen());

    await act(async () => {
      result.current.viewSession("sess-bad-agent");
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.viewingSessionMeta?.agentRunId).toBe("run-bad-agent");
    expect(result.current.viewingSessionMeta?.agentName).toBeNull();
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
});
