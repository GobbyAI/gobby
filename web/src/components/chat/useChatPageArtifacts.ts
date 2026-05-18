import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";

import { useArtifacts } from "../../hooks/useArtifacts";
import type { Artifact, ArtifactType } from "../../types/artifacts";
import type { ChatState } from "../../types/chat";
import type { ActivityTab } from "../activity/ActivityPanelTabs";

const VALID_ARTIFACT_TYPES = new Set<string>([
  "code",
  "text",
  "image",
  "sheet",
]);

interface UseChatPageArtifactsArgs {
  chat: ChatState;
  showTab: (tab: ActivityTab) => void;
  dismissOnMobile: () => void;
  closeIfAutoOpened: () => void;
  showPlanRef?: MutableRefObject<(() => void) | null>;
}

export interface UseChatPageArtifactsResult {
  artifacts: Map<string, Artifact>;
  activeArtifact: Artifact | null;
  openArtifact: (id: string) => void;
  updateArtifact: (id: string, content: string, messageId?: string) => void;
  setVersion: (id: string, index: number) => void;
  openCodeAsArtifact: (
    language: string,
    content: string,
    title?: string,
  ) => void;
  openFileAsArtifact: (
    type: ArtifactType,
    language: string,
    content: string,
    title?: string,
  ) => void;
  handleCloseArtifact: () => void;
  handleApprovePlan: () => void;
  handleRequestPlanChanges: (feedback: string) => void;
  planPendingApproval: boolean;
}

interface PlanArtifactState {
  switchKey: number;
  planArtifactId: string | null;
  pendingPlanArtifactId: string | null;
}

function emptyPlanArtifactState(switchKey: number): PlanArtifactState {
  return {
    switchKey,
    planArtifactId: null,
    pendingPlanArtifactId: null,
  };
}

export function useChatPageArtifacts({
  chat,
  showTab,
  dismissOnMobile,
  closeIfAutoOpened,
  showPlanRef,
}: UseChatPageArtifactsArgs): UseChatPageArtifactsResult {
  const {
    artifacts,
    activeArtifact,
    createArtifact,
    updateArtifact,
    openArtifact,
    closePanel: closeArtifactPanel,
    clearArtifacts,
    setVersion,
  } = useArtifacts();

  const conversationSwitchKey = chat.conversationSwitchKey ?? 0;
  const [planState, setPlanState] = useState<PlanArtifactState>(() =>
    emptyPlanArtifactState(conversationSwitchKey),
  );
  const planArtifactIdRef = useRef<string | null>(null);
  const planStateRef = useRef<PlanArtifactState>(planState);
  const lastPlanArtifactContentRef = useRef<string | null>(null);

  const planArtifactId =
    planState.switchKey === conversationSwitchKey ? planState.planArtifactId : null;
  const pendingPlanArtifactId =
    planState.switchKey === conversationSwitchKey
      ? planState.pendingPlanArtifactId
      : null;

  useEffect(() => {
    planStateRef.current = planState;
    planArtifactIdRef.current = planArtifactId;
  }, [planArtifactId, planState]);

  useEffect(() => {
    clearArtifacts();
    planArtifactIdRef.current = null;
    planStateRef.current = emptyPlanArtifactState(conversationSwitchKey);
    lastPlanArtifactContentRef.current = null;
  }, [conversationSwitchKey, clearArtifacts]);

  const openCodeAsArtifact = useCallback(
    (language: string, content: string, title?: string) => {
      createArtifact("code", content, language, title);
      showTab("artifacts");
    },
    [createArtifact, showTab],
  );

  const openFileAsArtifact = useCallback(
    (type: ArtifactType, language: string, content: string, title?: string) => {
      createArtifact(type, content, language, title);
      showTab("artifacts");
    },
    [createArtifact, showTab],
  );

  const onPlanReady = useCallback(
    (content: string | null) => {
      if (!content) return;
      const currentPlanState = planStateRef.current;
      const existingArtifactId =
        currentPlanState.switchKey === conversationSwitchKey
          ? currentPlanState.planArtifactId
          : null;
      if (
        existingArtifactId &&
        content === lastPlanArtifactContentRef.current
      ) {
        const nextPlanState: PlanArtifactState = {
          switchKey: conversationSwitchKey,
          planArtifactId: existingArtifactId,
          pendingPlanArtifactId: existingArtifactId,
        };
        planStateRef.current = nextPlanState;
        setPlanState(nextPlanState);
        openArtifact(existingArtifactId);
        showTab("plans");
        return;
      }

      const headingMatch = content.match(/^#\s+(.+)$/m);
      const title = headingMatch?.[1]?.trim() || "Implementation Plan";
      let nextArtifactId = existingArtifactId;

      if (existingArtifactId && artifacts.has(existingArtifactId)) {
        updateArtifact(existingArtifactId, content);
        openArtifact(existingArtifactId);
      } else {
        nextArtifactId = createArtifact("text", content, "markdown", title, {
          isPlan: true,
        });
      }
      planArtifactIdRef.current = nextArtifactId;
      lastPlanArtifactContentRef.current = content;
      const nextPlanState: PlanArtifactState = {
        switchKey: conversationSwitchKey,
        planArtifactId: nextArtifactId,
        pendingPlanArtifactId: nextArtifactId,
      };
      planStateRef.current = nextPlanState;
      setPlanState(nextPlanState);
      showTab("plans");
    },
    [
      artifacts,
      conversationSwitchKey,
      createArtifact,
      openArtifact,
      showTab,
      updateArtifact,
    ],
  );

  useEffect(() => {
    chat.setOnPlanReady?.(onPlanReady);
  }, [chat, onPlanReady]);

  const onArtifactEvent = useCallback(
    (type: string, content: string, language?: string, title?: string) => {
      if (VALID_ARTIFACT_TYPES.has(type)) {
        createArtifact(type as ArtifactType, content, language, title);
        showTab("artifacts");
      }
    },
    [createArtifact, showTab],
  );

  useEffect(() => {
    chat.setOnArtifactEvent?.(onArtifactEvent);
  }, [chat, onArtifactEvent]);

  const handleApprovePlan = useCallback(() => {
    setPlanState((prev) => {
      const nextPlanState =
        prev.switchKey === conversationSwitchKey
          ? { ...prev, pendingPlanArtifactId: null }
          : emptyPlanArtifactState(conversationSwitchKey);
      planStateRef.current = nextPlanState;
      return nextPlanState;
    });
    chat.onApprovePlan?.();
    dismissOnMobile();
  }, [chat, conversationSwitchKey, dismissOnMobile]);

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      setPlanState((prev) => {
        const nextPlanState =
          prev.switchKey === conversationSwitchKey
            ? { ...prev, pendingPlanArtifactId: null }
            : emptyPlanArtifactState(conversationSwitchKey);
        planStateRef.current = nextPlanState;
        return nextPlanState;
      });
      chat.onRequestPlanChanges?.(feedback);
      dismissOnMobile();
    },
    [chat, conversationSwitchKey, dismissOnMobile],
  );

  const handleCloseArtifact = useCallback(() => {
    closeArtifactPanel();
    closeIfAutoOpened();
  }, [closeArtifactPanel, closeIfAutoOpened]);

  useEffect(() => {
    if (showPlanRef) {
      showPlanRef.current = () => {
        if (planArtifactId) {
          openArtifact(planArtifactId);
          showTab("plans");
        }
      };
    }
    return () => {
      if (showPlanRef) showPlanRef.current = null;
    };
  }, [openArtifact, planArtifactId, showPlanRef, showTab]);

  const planPendingApproval =
    (pendingPlanArtifactId === activeArtifact?.id || chat.planPendingApproval) &&
    activeArtifact?.id === planArtifactId &&
    activeArtifact?.type === "text";

  return {
    artifacts,
    activeArtifact,
    openArtifact,
    updateArtifact,
    setVersion,
    openCodeAsArtifact,
    openFileAsArtifact,
    handleCloseArtifact,
    handleApprovePlan,
    handleRequestPlanChanges,
    planPendingApproval,
  };
}
