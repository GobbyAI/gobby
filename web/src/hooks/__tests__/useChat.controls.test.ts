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
});
