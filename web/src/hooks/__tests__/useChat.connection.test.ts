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

describe("useChat connection lifecycle", () => {
  it("connects to WebSocket on mount", async () => {
    await loadModule();
    renderHook(() => useChat());

    expect(mockWs.instances).toHaveLength(1);
    expect(mockWs.instances[0].url).toContain("/ws");
  });

  it("sets isConnected when WS opens", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    act(() => mockWs.instances[0].simulateOpen());

    expect(result.current.isConnected).toBe(true);
  });

  it("does not open a duplicate socket while the current socket is connecting", async () => {
    await loadModule();
    renderHook(() => useChat());

    expect(mockWs.instances).toHaveLength(1);
    expect(mockWs.instances[0].readyState).toBe(WebSocket.CONNECTING);

    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });

    expect(mockWs.instances).toHaveLength(1);
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
    expect(msg.events).toContain("session_usage_updated");
    expect(msg.events).toContain("token_event");
  });

  it("rebinds the active conversation with a heartbeat on reconnect", async () => {
    vi.useFakeTimers();
    try {
      await loadModule();
      renderHook(() => useChat());

      const ws = mockWs.instances[0];
      act(() => ws.simulateOpen());

      act(() => ws.simulateClose());
      act(() => {
        vi.advanceTimersByTime(2000);
      });

      expect(mockWs.instances).toHaveLength(2);

      const reconnected = mockWs.instances[1];
      act(() => reconnected.simulateOpen());

      const sentPayloads = reconnected.send.mock.calls.map(([raw]) =>
        JSON.parse(raw as string),
      );
      expect(sentPayloads[0].type).toBe("subscribe");
      expect(sentPayloads[1]).toEqual({
        type: "heartbeat",
        conversation_id: "test-conversation-id",
      });
      expect(sentPayloads.some((payload) => payload.type === "set_mode")).toBe(
        false,
      );
      expect(
        sentPayloads.some((payload) => payload.type === "set_project"),
      ).toBe(false);
      expect(sentPayloads.some((payload) => payload.type === "set_agent")).toBe(
        false,
      );
    } finally {
      vi.useRealTimers();
    }
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

  it("does not send set_project on connect when restoring an existing main chat", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    act(() => {
      result.current.setProjectIdRef("test-project-123");
    });

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const calls = ws.send.mock.calls.map((c) => JSON.parse(c[0]));
    const projectMsg = calls.find((m) => m.type === "set_project");

    expect(projectMsg).toBeUndefined();
  });

  it("keeps malformed JSON local to the router", async () => {
    vi.useFakeTimers();
    try {
      await loadModule();
      const { result } = renderHook(() => useChat());

      const ws = mockWs.instances[0];
      act(() => ws.simulateOpen());
      act(() => ws.simulateMessage("{"));
      act(() => {
        vi.advanceTimersByTime(2000);
      });

      expect(result.current.transportError).toBeNull();
      expect(mockWs.instances).toHaveLength(1);
      expect(ws.close).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports and reconnects after unexpected routing exceptions", async () => {
    vi.useFakeTimers();
    vi.doMock("../useChat/transportRouter", () => ({
      routeTransportMessage: () => {
        throw new Error("boom");
      },
    }));
    try {
      await loadModule();
      const { result } = renderHook(() => useChat());

      const ws = mockWs.instances[0];
      act(() => ws.simulateOpen());
      act(() => ws.simulateMessage({ type: "chat_stream" }));

      expect(result.current.transportError?.message).toBe(
        "Transport message handling failed; reconnecting",
      );
      expect(result.current.isConnected).toBe(false);
      expect(result.current.isReconnecting).toBe(true);
      expect(ws.close).toHaveBeenCalled();

      act(() => {
        vi.advanceTimersByTime(2000);
      });

      expect(mockWs.instances).toHaveLength(2);
    } finally {
      vi.doUnmock("../useChat/transportRouter");
      vi.useRealTimers();
    }
  });
});
