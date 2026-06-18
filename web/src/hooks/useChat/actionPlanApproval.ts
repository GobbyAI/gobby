/* eslint-disable react-hooks/exhaustive-deps -- Extracted useChat callbacks intentionally close over parent refs and stable setters to preserve the original hook behavior. */
import { useCallback } from "react";
import type {
  ApprovePlanAction,
  PlanApprovalActions,
  RequestPlanChangesAction,
  UseChatActionsParams,
} from "./actionTypes";

export function usePlanApprovalActions(
  params: UseChatActionsParams,
): PlanApprovalActions {
  const {
    attachedSessionIdRef,
    attachedSessionMetaRef,
    conversationIdRef,
    pendingPlanFeedbackRef,
    planContentRef,
    sessionInteractionModeRef,
    setPlanPendingApproval,
    wsRef,
  } = params;

  const isAttachedProxyTerminal = useCallback(
    () =>
      !!attachedSessionIdRef.current &&
      sessionInteractionModeRef.current === "proxy" &&
      attachedSessionMetaRef.current?.sessionType === "terminal",
    [],
  );

  const approvePlan: ApprovePlanAction = useCallback((option) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const proxySessionId = attachedSessionIdRef.current;
    const isProxyTerminal = isAttachedProxyTerminal();
    if (isProxyTerminal) {
      wsRef.current.send(
        JSON.stringify({
          type: "plan_approval_response",
          target_session_id: proxySessionId,
          decision: "approve",
          ...(option?.id ? { option_id: option.id } : {}),
        }),
      );
      setPlanPendingApproval(false);
      planContentRef.current = null;
      return;
    }
    if (!conversationIdRef.current) return;
    if (!planContentRef.current) return;
    wsRef.current.send(
      JSON.stringify({
        type: "plan_approval_response",
        conversation_id: conversationIdRef.current,
        decision: "approve",
        ...(option?.id ? { option_id: option.id } : {}),
      }),
    );
  }, [isAttachedProxyTerminal]);

  const requestPlanChanges: RequestPlanChangesAction = useCallback(
    (feedback) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      const proxySessionId = attachedSessionIdRef.current;
      const isProxyTerminal = isAttachedProxyTerminal();
      if (isProxyTerminal) {
        setPlanPendingApproval(false);
        planContentRef.current = null;
        wsRef.current.send(
          JSON.stringify({
            type: "plan_approval_response",
            target_session_id: proxySessionId,
            decision: "request_changes",
            feedback,
          }),
        );
        return;
      }
      if (!conversationIdRef.current) return;
      if (!planContentRef.current) return;
      pendingPlanFeedbackRef.current = feedback;
      setPlanPendingApproval(false);
      planContentRef.current = null;
      wsRef.current.send(
        JSON.stringify({
          type: "plan_approval_response",
          conversation_id: conversationIdRef.current,
          decision: "request_changes",
          feedback,
        }),
      );
    },
    [isAttachedProxyTerminal],
  );

  return {
    approvePlan,
    requestPlanChanges,
  };
}
