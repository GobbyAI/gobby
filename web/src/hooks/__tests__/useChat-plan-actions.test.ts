import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ApprovalOption } from "../../types/chat";
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

function planApprovalResponses(
  ws: UseChatTestContext["mockWs"]["instances"][number],
) {
  return ws.send.mock.calls
    .map((call) => JSON.parse(call[0] as string))
    .filter((msg) => msg.type === "plan_approval_response");
}

describe("useChat plan actions", () => {
  it("approvePlan sends a plan_approval_response with decision approve", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        tool_call_id: "plan-tool",
        plan_content: "# My Plan\n\nStep 1...",
      });
    });
    expect(result.current.planPendingApproval).toBe(true);

    act(() => {
      result.current.approvePlan();
    });

    const responses = planApprovalResponses(ws);
    expect(responses).toHaveLength(1);
    expect(responses[0].decision).toBe("approve");
    expect(responses[0].conversation_id).toBe(result.current.conversationId);
    expect(responses[0].tool_call_id).toBe("plan-tool");
    // Approve carries no feedback, and the backend mode_changed event is
    // authoritative for clearing — the UI stays pending until it arrives.
    expect(responses[0].feedback).toBeUndefined();
    expect(result.current.planPendingApproval).toBe(true);
  });

  it("approvePlan is a no-op when no plan is pending", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.approvePlan();
    });

    expect(planApprovalResponses(ws)).toHaveLength(0);
  });

  it("requestPlanChanges is a no-op when no plan is pending", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      result.current.requestPlanChanges("please revise");
    });

    expect(planApprovalResponses(ws)).toHaveLength(0);
    expect(result.current.planPendingApproval).toBe(false);
  });

  it("approvePlan does not send when the socket is not open", async () => {
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
    expect(result.current.planPendingApproval).toBe(true);

    // Drop the socket without firing onclose (avoids triggering reconnect),
    // isolating the readyState guard from the pending-plan guard.
    act(() => {
      ws.readyState = WebSocket.CLOSED;
      result.current.approvePlan();
    });

    expect(planApprovalResponses(ws)).toHaveLength(0);
  });

  it("requestPlanChanges sends decision request_changes with feedback and clears the UI", async () => {
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
    expect(result.current.planPendingApproval).toBe(true);

    const feedback = "Please tighten Step 1";
    act(() => {
      result.current.requestPlanChanges(feedback);
    });

    const responses = planApprovalResponses(ws);
    expect(responses).toHaveLength(1);
    expect(responses[0].decision).toBe("request_changes");
    expect(responses[0].feedback).toBe(feedback);
    expect(responses[0].conversation_id).toBe(result.current.conversationId);
    // request_changes eagerly clears the approval UI to avoid a ghost flash.
    expect(result.current.planPendingApproval).toBe(false);
  });

  it("captures accept options and sends option_id when an option is selected", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const options: ApprovalOption[] = [
      {
        id: "approve_yolo",
        label: "Approve (YOLO)",
        decision: "approve",
        emphasis: "primary",
      },
      {
        id: "approve_act",
        label: "Approve (Act)",
        decision: "approve",
        emphasis: "accent",
      },
    ];
    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        plan_content: "# My Plan\n\nStep 1...",
        source: "claude",
        options,
      });
    });
    expect(result.current.planApprovalOptions).toEqual(options);

    act(() => {
      result.current.approvePlan(options[0]);
    });

    const responses = planApprovalResponses(ws);
    expect(responses).toHaveLength(1);
    expect(responses[0].decision).toBe("approve");
    expect(responses[0].option_id).toBe("approve_yolo");
    // An approve option leaves clearing to the backend mode_changed event.
    expect(result.current.planPendingApproval).toBe(true);
  });

  it("plan_pending_approval sets pending state and fires onPlanReady", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const onPlanReady = vi.fn();
    act(() => result.current.setOnPlanReady(onPlanReady));

    const planContent = "# My Plan\n\nStep 1...";
    act(() => {
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        plan_content: planContent,
      });
    });

    expect(result.current.planPendingApproval).toBe(true);
    expect(onPlanReady).toHaveBeenCalledTimes(1);
    expect(onPlanReady).toHaveBeenCalledWith(planContent);
  });

  it("mode_changed plan_approved clears the plan and flips the Plan radio off", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    const onModeChanged = vi.fn();
    act(() => result.current.setOnModeChanged(onModeChanged));

    // Enter plan mode (Plan radio on), then surface a pending plan.
    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "plan",
        conversation_id: result.current.conversationId,
      });
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        plan_content: "# My Plan\n\nStep 1...",
      });
    });
    expect(result.current.planPendingApproval).toBe(true);

    // Approval arrives from the backend: plan state clears and the chat mode
    // leaves plan, so the Plan radio flips off.
    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "normal",
        reason: "plan_approved",
        conversation_id: result.current.conversationId,
      });
    });

    expect(result.current.planPendingApproval).toBe(false);
    expect(onModeChanged).toHaveBeenLastCalledWith("normal");
  });

  it("mode_changed plan_approval_timed_out dismisses the pending plan", async () => {
    await loadModule();
    const { result } = renderHook(() => useChat());

    const ws = mockWs.instances[0];
    act(() => ws.simulateOpen());

    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "plan",
        conversation_id: result.current.conversationId,
      });
      ws.simulateMessage({
        type: "plan_pending_approval",
        conversation_id: result.current.conversationId,
        plan_content: "# My Plan\n\nStep 1...",
        options: [{ id: "approve", label: "Approve" }],
      });
    });
    expect(result.current.planPendingApproval).toBe(true);
    expect(result.current.planApprovalOptions).toHaveLength(1);

    act(() => {
      ws.simulateMessage({
        type: "mode_changed",
        mode: "plan",
        reason: "plan_approval_timed_out",
        conversation_id: result.current.conversationId,
      });
    });

    expect(result.current.planPendingApproval).toBe(false);
    expect(result.current.planApproved).toBe(false);
    expect(result.current.planApprovalOptions).toEqual([]);
  });
});
