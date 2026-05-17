import { useCallback, useRef, useState } from "react";
import type { ChatMode } from "../../types/chat";

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
  const planContentRef = useRef<string | null>(null);
  const currentModeRef = useRef<ChatMode>("plan");

  const onModeChangedRef = useRef<ModeChangedCallback | null>(null);
  const setOnModeChanged = useCallback((fn: ModeChangedCallback) => {
    onModeChangedRef.current = fn;
  }, []);

  const onPlanReadyRef = useRef<PlanReadyCallback | null>(null);
  const setOnPlanReady = useCallback((fn: PlanReadyCallback) => {
    onPlanReadyRef.current = fn;
  }, []);

  const onArtifactEventRef = useRef<ArtifactEventCallback | null>(null);
  const setOnArtifactEvent = useCallback((fn: ArtifactEventCallback) => {
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

  const pendingPlanFeedbackRef = useRef<string | null>(null);

  return {
    currentModeRef,
    onArtifactEventRef,
    onChatClearedRef,
    onChatDeletedRef,
    onModeChangedRef,
    onPlanReadyRef,
    pendingPlanFeedbackRef,
    planContentRef,
    planPendingApproval,
    setOnArtifactEvent,
    setOnChatCleared,
    setOnChatDeleted,
    setOnModeChanged,
    setOnPlanReady,
    setPlanPendingApproval,
  };
}
