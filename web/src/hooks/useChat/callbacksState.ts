import { useCallback, useRef, useState } from "react";
import type { ApprovalOption, ChatMode } from "../../types/chat";
import { normalizeChatMode } from "../../types/chat";

type ModeChangedCallback = (mode: ChatMode) => void;
type PlanReadyCallback = (content: string | null) => void;
type ArtifactEventCallback = (
  type: string,
  content: string,
  language?: string,
  title?: string,
) => void;
type ChatLifecycleCallback = (conversationId: string) => void;

export function usePlanArtifactCallbacks() {
  const [planPendingApproval, setPlanPendingApproval] = useState(false);
  // Authoritative approval signal: true ONLY after the backend plan_approved
  // event. Reset on each fresh plan_pending_approval and on conversation
  // switch. Lets the Plans panel tell approve from reject (#15681).
  const [planApproved, setPlanApproved] = useState(false);
  // Per-CLI plan-accept options carried in the plan_pending_approval payload.
  const [planApprovalOptions, setPlanApprovalOptions] = useState<ApprovalOption[]>([]);
  const planContentRef = useRef<string | null>(null);
  const planToolCallIdRef = useRef<string | null>(null);
  const currentModeRef = useRef<ChatMode>("plan");

  const onModeChangedRef = useRef<ModeChangedCallback | null>(null);
  const setOnModeChanged = useCallback((fn: ModeChangedCallback) => {
    onModeChangedRef.current = fn;
  }, []);
  const setCurrentMode = useCallback((mode: ChatMode) => {
    const normalizedMode = normalizeChatMode(mode);
    currentModeRef.current = normalizedMode;
    onModeChangedRef.current?.(normalizedMode);
  }, []);

  const onPlanReadyRef = useRef<PlanReadyCallback | null>(null);
  const setOnPlanReady = useCallback((fn: PlanReadyCallback) => {
    onPlanReadyRef.current = fn;
  }, []);

  const onArtifactEventRef = useRef<ArtifactEventCallback | null>(null);
  const setOnArtifactEvent = useCallback((fn: ArtifactEventCallback | null) => {
    onArtifactEventRef.current = fn;
  }, []);

  const onChatDeletedRef = useRef<ChatLifecycleCallback | null>(null);
  const setOnChatDeleted = useCallback((fn: ChatLifecycleCallback) => {
    onChatDeletedRef.current = fn;
  }, []);

  const onChatClearedRef = useRef<ChatLifecycleCallback | null>(null);
  const setOnChatCleared = useCallback((fn: ChatLifecycleCallback) => {
    onChatClearedRef.current = fn;
  }, []);

  // Internal handoff between plan-approval actions and transport handlers;
  // actions set it, the next outbound chat frame consumes and clears it.
  const pendingPlanFeedbackRef = useRef<string | null>(null);

  return {
    currentModeRef,
    onArtifactEventRef,
    onChatClearedRef,
    onChatDeletedRef,
    onModeChangedRef,
    onPlanReadyRef,
    pendingPlanFeedbackRef,
    planApprovalOptions,
    planApproved,
    planContentRef,
    planToolCallIdRef,
    planPendingApproval,
    setOnArtifactEvent,
    setOnChatCleared,
    setOnChatDeleted,
    setCurrentMode,
    setOnModeChanged,
    setOnPlanReady,
    setPlanApprovalOptions,
    setPlanApproved,
    setPlanPendingApproval,
  };
}
