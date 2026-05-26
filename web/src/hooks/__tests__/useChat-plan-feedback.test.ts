import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  cleanupUseChatTestContext(context);
});

async function loadModule() {
  useChat = await loadUseChatModule();
}

describe("useChat plan feedback", () => {
  it("auto-sends feedback message on plan_changes_requested", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    // 1. Simulate a plan pending approval
    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        plan_content: "# My Plan\n\nStep 1...",
      });
    });
    expect(result.current.planPendingApproval).toBe(true);

    // 2. Request changes with feedback
    const feedback = "Please add more detail to Step 1";
    act(() => {
      result.current.requestPlanChanges(feedback);
    });

    // Should have sent plan_approval_response
    const sentMsg = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(sentMsg.type).toBe("plan_approval_response");
    expect(sentMsg.decision).toBe("request_changes");
    expect(sentMsg.feedback).toBe(feedback);

    // Approval UI should be cleared immediately
    expect(result.current.planPendingApproval).toBe(false);

    // 3. Simulate backend confirming the change with mode_changed
    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "plan",
        reason: "plan_changes_requested",
        conversation_id: result.current.conversationId,
      });
    });

    // 4. Advance timers to trigger auto-send setTimeout
    // Use async act to flush microtasks from sendMessage state updates
    await act(async () => {
      vi.advanceTimersByTime(200);
    });

    // Should have sent the feedback as a chat message
    const lastSent = JSON.parse(
      ws.send.mock.calls[ws.send.mock.calls.length - 1][0],
    );
    expect(lastSent.type).toBe("chat_message");
    expect(lastSent.content).toBe(feedback);

    // Messages should include the feedback
    const userMsgs = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgs).toHaveLength(1);
    expect(userMsgs[0].content).toBe(feedback);
  });
});
