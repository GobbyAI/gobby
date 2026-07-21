import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";

import { usePlans } from "../../hooks/usePlans";
import type { Plan } from "../../types/plans";
import type { ApprovalOption, ChatState } from "../../types/chat";
import type { ActivityTab } from "../activity/ActivityPanelTabs";

interface UseChatPagePlansArgs {
  chat: ChatState;
  showTab: (tab: ActivityTab) => void;
  dismissOnMobile: () => void;
  showPlanRef?: MutableRefObject<(() => void) | null>;
}

export interface UseChatPagePlansResult {
  plans: Map<string, Plan>;
  activePlan: Plan | null;
  openPlan: (id: string) => void;
  setPlanVersion: (id: string, index: number) => void;
  handleApprovePlan: (option?: ApprovalOption) => void;
  handleRequestPlanChanges: (feedback: string) => void;
  planPendingApproval: boolean;
  planApproved: boolean;
  planApprovalOptions?: ApprovalOption[];
}

interface ConversationPlanState {
  switchKey: number;
  planId: string | null;
  pendingPlanId: string | null;
}

function emptyConversationPlanState(switchKey: number): ConversationPlanState {
  return {
    switchKey,
    planId: null,
    pendingPlanId: null,
  };
}

export function useChatPagePlans({
  chat,
  showTab,
  dismissOnMobile,
  showPlanRef,
}: UseChatPagePlansArgs): UseChatPagePlansResult {
  const {
    plans,
    activePlan,
    createPlan,
    updatePlan,
    openPlan,
    clearPlans,
    setPlanVersion,
  } = usePlans();

  const conversationSwitchKey = chat.conversationSwitchKey ?? 0;
  const [planState, setPlanState] = useState<ConversationPlanState>(() =>
    emptyConversationPlanState(conversationSwitchKey),
  );
  const planStateRef = useRef<ConversationPlanState>(planState);
  const lastPlanContentRef = useRef<string | null>(null);
  const prevChatPendingRef = useRef(chat.planPendingApproval);

  const planId =
    planState.switchKey === conversationSwitchKey ? planState.planId : null;
  const pendingPlanId =
    planState.switchKey === conversationSwitchKey ? planState.pendingPlanId : null;

  useEffect(() => {
    planStateRef.current = planState;
  }, [planState]);

  useEffect(() => {
    const wasPending = prevChatPendingRef.current;
    prevChatPendingRef.current = chat.planPendingApproval;
    if (wasPending && !chat.planPendingApproval) {
      setPlanState((previous) =>
        previous.switchKey === conversationSwitchKey && previous.pendingPlanId !== null
          ? { ...previous, pendingPlanId: null }
          : previous,
      );
    }
  }, [chat.planPendingApproval, conversationSwitchKey]);

  useEffect(() => {
    clearPlans();
    const emptyState = emptyConversationPlanState(conversationSwitchKey);
    planStateRef.current = emptyState;
    lastPlanContentRef.current = null;
  }, [conversationSwitchKey, clearPlans]);

  const onPlanReady = useCallback(
    (content: string | null) => {
      if (!content) return;
      const currentPlanState = planStateRef.current;
      const existingPlanId =
        currentPlanState.switchKey === conversationSwitchKey
          ? currentPlanState.planId
          : null;

      if (existingPlanId && content === lastPlanContentRef.current) {
        const nextPlanState: ConversationPlanState = {
          switchKey: conversationSwitchKey,
          planId: existingPlanId,
          pendingPlanId: existingPlanId,
        };
        planStateRef.current = nextPlanState;
        setPlanState(nextPlanState);
        openPlan(existingPlanId);
        showTab("plans");
        return;
      }

      const headingMatch = content.match(/^#\s+(.+)$/m);
      const title = headingMatch?.[1]?.trim() || "Implementation Plan";
      let nextPlanId: string;

      if (existingPlanId && plans.has(existingPlanId)) {
        updatePlan(existingPlanId, content);
        openPlan(existingPlanId);
        nextPlanId = existingPlanId;
      } else {
        nextPlanId = createPlan(content, title);
      }

      lastPlanContentRef.current = content;
      const nextPlanState: ConversationPlanState = {
        switchKey: conversationSwitchKey,
        planId: nextPlanId,
        pendingPlanId: nextPlanId,
      };
      planStateRef.current = nextPlanState;
      setPlanState(nextPlanState);
      showTab("plans");
    },
    [conversationSwitchKey, createPlan, openPlan, plans, showTab, updatePlan],
  );

  useEffect(() => {
    chat.setOnPlanReady?.(onPlanReady);
  }, [chat, onPlanReady]);

  const clearCurrentConversationPendingPlan = useCallback(() => {
    setPlanState((previous) => {
      const nextPlanState =
        previous.switchKey === conversationSwitchKey
          ? { ...previous, pendingPlanId: null }
          : emptyConversationPlanState(conversationSwitchKey);
      planStateRef.current = nextPlanState;
      return nextPlanState;
    });
  }, [conversationSwitchKey]);

  const handleApprovePlan = useCallback(
    (option?: ApprovalOption) => {
      clearCurrentConversationPendingPlan();
      chat.onApprovePlan?.(option);
      dismissOnMobile();
    },
    [chat, clearCurrentConversationPendingPlan, dismissOnMobile],
  );

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      clearCurrentConversationPendingPlan();
      chat.onRequestPlanChanges?.(feedback);
      dismissOnMobile();
    },
    [chat, clearCurrentConversationPendingPlan, dismissOnMobile],
  );

  useEffect(() => {
    if (showPlanRef) {
      showPlanRef.current = () => {
        if (planId) {
          openPlan(planId);
          showTab("plans");
        }
      };
    }
    return () => {
      if (showPlanRef) showPlanRef.current = null;
    };
  }, [openPlan, planId, showPlanRef, showTab]);

  const planPendingApproval =
    (pendingPlanId === activePlan?.id || chat.planPendingApproval) &&
    activePlan?.id === planId;

  return {
    plans,
    activePlan,
    openPlan,
    setPlanVersion,
    handleApprovePlan,
    handleRequestPlanChanges,
    planPendingApproval,
    planApproved: chat.planApproved,
    planApprovalOptions: chat.planApprovalOptions,
  };
}
