import { describe, it, expect, beforeEach, afterEach } from "vitest";
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

describe("useChat project and mode controls", () => {
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

  it("sendMode skips redundant set_mode when the mode is unchanged", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    // Move off the initial "plan" mode so the second call below is the true
    // redundant-emission case under test.
    act(() => {
      result.current.sendMode("normal");
    });
    ws.send.mockClear();

    act(() => {
      result.current.sendMode("normal");
    });

    const setModeMsgs = ws.send.mock.calls
      .map((c) => JSON.parse(c[0]))
      .filter((m) => m.type === "set_mode");
    expect(setModeMsgs).toHaveLength(0);
  });

  it("sendMode sends a message when the mode actually changes", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());
    ws.send.mockClear();

    act(() => {
      result.current.sendMode("normal");
    });
    act(() => {
      result.current.sendMode("plan");
    });

    const setModeMsgs = ws.send.mock.calls
      .map((c) => JSON.parse(c[0]))
      .filter((m) => m.type === "set_mode");
    expect(setModeMsgs).toHaveLength(2);
    expect(setModeMsgs.map((m) => m.mode)).toEqual(["normal", "plan"]);
  });

  it("sendMode treats accept_edits as normal for the no-op guard", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.sendMode("normal");
    });
    ws.send.mockClear();

    act(() => {
      result.current.sendMode("accept_edits");
    });

    const setModeMsgs = ws.send.mock.calls
      .map((c) => JSON.parse(c[0]))
      .filter((m) => m.type === "set_mode");
    expect(setModeMsgs).toHaveLength(0);
  });

  it("updates ACP session metadata, mode, and usage on existing surfaces", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    const modeChanges: string[] = [];
    act(() => ws.simulateOpen());
    act(() => {
      result.current.setOnModeChanged((mode) => modeChanges.push(mode));
    });

    act(() => {
      ws.simulateMessage({
        type: "session_info",
        conversation_id: result.current.conversationId,
        db_session_id: "db-1",
        session_title: "ACP title",
        updated_at: "2026-06-27T05:00:00Z",
      });
    });

    expect(result.current.sessionTitle).toBe("ACP title");

    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        conversation_id: result.current.conversationId,
        mode: "yolo",
        reason: "acp_current_mode_update",
      });
    });

    expect(modeChanges[modeChanges.length - 1]).toBe("bypass");

    act(() => {
      ws.simulateMessage({
        type: "session_usage_updated",
        session_id: "db-1",
        context_window: 1000,
        context_used_tokens: 250,
        context_usage_ratio: 0.25,
        context_usage_source: "acp",
        context_usage_confidence: "reported",
        updated_at: "2026-06-27T05:01:00Z",
      });
    });

    expect(result.current.contextUsage.totalInputTokens).toBe(250);
    expect(result.current.contextUsage.contextWindow).toBe(1000);
    expect(result.current.contextUsage.contextUsageRatio).toBe(0.25);
    expect(result.current.contextUsage.contextUsageSource).toBe("acp");
    expect(result.current.contextUsage.contextUsageConfidence).toBe("reported");
  });
});
