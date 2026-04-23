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

describe("useChat streaming and event handling", () => {
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

  it("keeps plan approval UI visible until plan_approved arrives", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        plan_content: "# My Plan\n\nStep 1...",
      });
    });

    act(() => {
      result.current.approvePlan();
    });

    expect(result.current.planPendingApproval).toBe(true);

    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "normal",
        reason: "plan_approved",
        conversation_id: result.current.conversationId,
      });
    });

    expect(result.current.planPendingApproval).toBe(false);
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

  it("updates context usage from session_usage_updated for the active session", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "session_usage_updated",
        session_id: "test-conversation-id",
        usage_input_tokens: 420,
        usage_output_tokens: 33,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 10,
        context_window: 200000,
      });
    });

    expect(result.current.contextUsage).toMatchObject({
      totalInputTokens: 420,
      outputTokens: 33,
      cacheReadTokens: 120,
      cacheCreationTokens: 10,
      uncachedInputTokens: 290,
      contextWindow: 200000,
    });
    expect(result.current.contextUsageUpdatedAt).not.toBeNull();
  });

  it("preserves existing totals when session_usage_updated omits fields", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "session_usage_updated",
        session_id: "test-conversation-id",
        usage_input_tokens: 420,
        usage_output_tokens: 33,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 10,
        context_window: 200000,
      });
    });

    act(() => {
      ws.simulateMessage({
        type: "session_usage_updated",
        session_id: "test-conversation-id",
        usage_output_tokens: 44,
      });
    });

    expect(result.current.contextUsage).toMatchObject({
      totalInputTokens: 420,
      outputTokens: 44,
      cacheReadTokens: 120,
      cacheCreationTokens: 10,
      uncachedInputTokens: 290,
      contextWindow: 200000,
    });
  });

  it("preserves existing totals when token_event session totals are partial", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "session_usage_updated",
        session_id: "test-conversation-id",
        usage_input_tokens: 420,
        usage_output_tokens: 33,
        usage_cache_read_tokens: 120,
        usage_cache_creation_tokens: 10,
        context_window: 200000,
      });
    });

    act(() => {
      ws.simulateMessage({
        type: "token_event",
        session_id: "test-conversation-id",
        event_at: "2026-04-08T12:00:00Z",
        session_totals: {
          output_tokens: 44,
        },
      });
    });

    expect(result.current.contextUsage).toMatchObject({
      totalInputTokens: 420,
      outputTokens: 44,
      cacheReadTokens: 120,
      cacheCreationTokens: 10,
      uncachedInputTokens: 290,
      contextWindow: 200000,
    });
  });
});
