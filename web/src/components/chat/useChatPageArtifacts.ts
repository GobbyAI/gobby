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

  const [planArtifactId, setPlanArtifactId] = useState<string | null>(null);
  const [pendingPlanArtifactId, setPendingPlanArtifactId] = useState<
    string | null
  >(null);
  const planArtifactIdRef = useRef<string | null>(null);
  const lastPlanArtifactContentRef = useRef<string | null>(null);

  useEffect(() => {
    planArtifactIdRef.current = planArtifactId;
  }, [planArtifactId]);

  const [prevSwitchKey, setPrevSwitchKey] = useState(
    chat.conversationSwitchKey,
  );
  if (prevSwitchKey !== chat.conversationSwitchKey) {
    setPrevSwitchKey(chat.conversationSwitchKey);
    setPlanArtifactId(null);
    setPendingPlanArtifactId(null);
  }

  useEffect(() => {
    clearArtifacts();
    planArtifactIdRef.current = null;
    lastPlanArtifactContentRef.current = null;
  }, [chat.conversationSwitchKey, clearArtifacts]);

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
      const existingArtifactId = planArtifactIdRef.current;
      if (
        existingArtifactId &&
        content === lastPlanArtifactContentRef.current
      ) {
        setPendingPlanArtifactId(existingArtifactId);
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
      setPlanArtifactId(nextArtifactId);
      setPendingPlanArtifactId(nextArtifactId);
      showTab("plans");
    },
    [artifacts, createArtifact, openArtifact, showTab, updateArtifact],
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
    setPendingPlanArtifactId(null);
    chat.onApprovePlan?.();
    dismissOnMobile();
  }, [chat, dismissOnMobile]);

  const handleRequestPlanChanges = useCallback(
    (feedback: string) => {
      setPendingPlanArtifactId(null);
      chat.onRequestPlanChanges?.(feedback);
      dismissOnMobile();
    },
    [chat, dismissOnMobile],
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
