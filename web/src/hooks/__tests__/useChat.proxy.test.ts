import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  cleanupUseChatTestContext,
  createUseChatTestContext,
  loadUseChatModule,
  type UseChatTestContext,
} from "./useChat.setup";

let context: UseChatTestContext;
let mockWs: UseChatTestContext["mockWs"];
let useChat: Awaited<ReturnType<typeof loadUseChatModule>>;

beforeEach(() => {
  context = createUseChatTestContext();
  mockWs = context.mockWs;
});

afterEach(() => {
  cleanupUseChatTestContext(context);
});

async function loadModule() {
  useChat = await loadUseChatModule();
}

describe("useChat proxy session messaging", () => {
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

  it("keeps optimistic proxy mapping when the ack has no message id", async () => {
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
      result.current.sendMessage("queued message");
    });

    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );

    act(() => {
      ws.simulateMessage({
        type: "send_to_cli_session_result",
        session_id: "sess-proxy",
        delivered: false,
        delivery_method: "hook_piggyback",
        client_message_id: sentMsg.client_message_id,
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe(`user-${sentMsg.client_message_id}`);

    act(() => {
      ws.simulateMessage({
        type: "session_message",
        session_id: "sess-proxy",
        message: {
          id: "db-msg-later",
          role: "user",
          content: "queued message",
          timestamp: "2026-04-17T20:01:00Z",
        },
      });
    });

    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0].id).toBe("db-msg-later");
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
});
