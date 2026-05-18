import { useCallback, useEffect, useState } from "react";

import type {
  ChatState,
  SessionObservationMeta,
} from "../../types/chat";
import {
  buildReasoningPreferenceKey,
  fetchProviderModelCatalog,
  getPreferredReasoningEffort,
  resolveProviderModelPair,
  type ProviderModelEntry,
} from "../../lib/providerModels";
import { canProxyAttachObservationMeta } from "../../lib/sessionProxyAttach";

interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
}

interface UseChatPageProviderStateArgs {
  chat: ChatState;
  mainSessionMeta: SessionObservationMeta | null;
  currentModel: string;
  reasoningPreferences: Record<string, string>;
  onModelChange?: (model: string) => void;
  onReasoningPreferenceChange?: (
    provider: string,
    model: string,
    reasoningEffort: string,
  ) => void;
  projectId?: string | null;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
}

export function useChatPageProviderState({
  chat,
  mainSessionMeta,
  currentModel,
  reasoningPreferences,
  onModelChange,
  onReasoningPreferenceChange,
  projectId,
  confirm,
}: UseChatPageProviderStateArgs) {
  const [availableProviders, setAvailableProviders] = useState<string[]>([]);
  const [providerModelCatalog, setProviderModelCatalog] = useState<
    ProviderModelEntry[]
  >([]);

  const viewingMeta = chat.viewingSessionMeta ?? chat.attachedSessionMeta ?? null;
  const isSwappedTerminal = viewingMeta?.sessionType === "terminal";
  const isAutonomousSession = Boolean(
    isSwappedTerminal && viewingMeta?.agentRunId,
  );
  const isProxyAttached =
    Boolean(chat.attachedSessionId) && chat.sessionInteractionMode === "proxy";
  const canAttachViewedSession =
    !isAutonomousSession && canProxyAttachObservationMeta(viewingMeta);
  const canControlViewedSession =
    viewingMeta?.sessionType === "terminal" && !isAutonomousSession;
  const providerPickerDisabledReason = isProxyAttached
    ? "Attached session owns provider, model, and reasoning"
    : isAutonomousSession
      ? chat.sessionInteractionMode === "proxy"
        ? "Cannot change provider on a pipeline-managed session"
        : "Observing autonomous session"
      : null;

  const mainInputSelection = resolveProviderModelPair(
    providerModelCatalog,
    {
      provider: mainSessionMeta?.source ?? null,
      model: mainSessionMeta?.model ?? null,
    },
    {
      provider: chat.provider ?? null,
      model: currentModel,
    },
  );
  const viewedInputSelection = resolveProviderModelPair(
    providerModelCatalog,
    {
      provider: viewingMeta?.source ?? null,
      model: viewingMeta?.model ?? null,
    },
    {
      provider: mainSessionMeta?.source ?? chat.provider ?? null,
      model: mainSessionMeta?.model ?? currentModel,
    },
  );
  const effectiveInputProvider = isSwappedTerminal
    ? viewedInputSelection.provider
    : mainInputSelection.provider;
  const effectiveInputModel = isSwappedTerminal
    ? (viewedInputSelection.model ?? "")
    : (mainInputSelection.model ?? "");
  const effectiveAgentName = isSwappedTerminal
    ? (viewingMeta?.agentName ?? chat.activeAgent)
    : chat.activeAgent;
  const effectiveBranch = viewingMeta?.gitBranch ?? chat.currentBranch;
  const effectiveReasoningPreferenceKey = buildReasoningPreferenceKey(
    effectiveInputProvider,
    effectiveInputModel,
  );
  const preferredReasoningEffort = effectiveReasoningPreferenceKey
    ? reasoningPreferences[effectiveReasoningPreferenceKey]
    : null;
  const effectiveInputReasoning =
    isSwappedTerminal && viewingMeta?.reasoningEffort
      ? viewingMeta.reasoningEffort
      : getPreferredReasoningEffort(
          providerModelCatalog,
          effectiveInputProvider,
          effectiveInputModel,
          preferredReasoningEffort,
        );

  const isReadOnlySession =
    isSwappedTerminal && chat.sessionInteractionMode !== "proxy";
  const showChatInput = !isReadOnlySession;
  const chatInputDisabled =
    !chat.isConnected || Boolean(chat.isContinuingSession);
  const chatInputDisabledPlaceholder = !chat.isConnected
    ? chat.isReconnecting
      ? "Reconnecting to server..."
      : "Connecting to server..."
    : chat.isContinuingSession
      ? "Resuming session in web chat..."
      : undefined;
  const chatInputDisabledAriaLabel = !chat.isConnected
    ? chat.isReconnecting
      ? "Message input — reconnecting"
      : "Message input — connecting"
    : chat.isContinuingSession
      ? "Message input — resuming session"
      : undefined;
  const handleInputModeChange =
    isProxyAttached && chat.onAttachedModeChange
      ? chat.onAttachedModeChange
      : chat.onModeChange;

  const handleResumeViewedSession = useCallback(() => {
    if (
      !isSwappedTerminal ||
      isAutonomousSession ||
      !chat.viewingSessionId ||
      !chat.continueSessionInChat
    ) {
      return;
    }
    if (effectiveInputModel) {
      onModelChange?.(effectiveInputModel);
    }
    if (
      effectiveInputProvider &&
      effectiveInputModel &&
      effectiveInputReasoning
    ) {
      onReasoningPreferenceChange?.(
        effectiveInputProvider,
        effectiveInputModel,
        effectiveInputReasoning,
      );
    }
    void chat.continueSessionInChat(
      chat.viewingSessionId,
      projectId ?? undefined,
      {
        provider: effectiveInputProvider,
        model: effectiveInputModel,
        reasoningEffort: effectiveInputReasoning,
        chatMode: viewingMeta?.chatMode ?? null,
        fallbackContext: "auto",
      },
    );
  }, [
    chat,
    effectiveInputModel,
    effectiveInputProvider,
    effectiveInputReasoning,
    isAutonomousSession,
    isSwappedTerminal,
    onModelChange,
    onReasoningPreferenceChange,
    projectId,
    viewingMeta?.chatMode,
  ]);

  const handleSwappedSessionProviderSelection = useCallback(
    async (provider: string, model: string, reasoningEffort: string | null) => {
      if (
        !isSwappedTerminal ||
        isAutonomousSession ||
        !chat.viewingSessionId ||
        !chat.continueSessionInChat
      ) {
        return;
      }

      const confirmChange = canAttachViewedSession
        ? await confirm({
            title: "Change provider?",
            description: `This will end the terminal session and resume the conversation with ${provider} ${model}.`,
            confirmLabel: "Change Provider",
            destructive: true,
          })
        : true;
      if (!confirmChange) return;

      chat.onProviderChange?.(provider);
      onModelChange?.(model);
      if (reasoningEffort) {
        onReasoningPreferenceChange?.(provider, model, reasoningEffort);
      }
      await chat.continueSessionInChat(
        chat.viewingSessionId,
        projectId ?? undefined,
        {
          provider,
          model,
          reasoningEffort,
          chatMode: viewingMeta?.chatMode ?? null,
          fallbackContext: "auto",
        },
      );
    },
    [
      chat,
      confirm,
      isAutonomousSession,
      isSwappedTerminal,
      onModelChange,
      onReasoningPreferenceChange,
      projectId,
      viewingMeta?.chatMode,
      canAttachViewedSession,
    ],
  );

  const handleMainProviderSelection = useCallback(
    (provider: string, model: string, reasoningEffort: string | null) => {
      const providerChanged =
        provider !== (effectiveInputProvider ?? chat.provider ?? "claude");

      onModelChange?.(model);
      if (reasoningEffort) {
        onReasoningPreferenceChange?.(provider, model, reasoningEffort);
      }

      if (!providerChanged) {
        return;
      }

      if (chat.onSwitchProvider) {
        chat.onSwitchProvider(provider, {
          model,
          reasoningEffort,
        });
        return;
      }

      chat.onProviderChange?.(provider);
    },
    [chat, effectiveInputProvider, onModelChange, onReasoningPreferenceChange],
  );

  const handleReasoningChange = useCallback(
    (reasoningEffort: string) => {
      if (effectiveInputProvider && effectiveInputModel) {
        onReasoningPreferenceChange?.(
          effectiveInputProvider,
          effectiveInputModel,
          reasoningEffort,
        );
      }
    },
    [effectiveInputModel, effectiveInputProvider, onReasoningPreferenceChange],
  );

  useEffect(() => {
    fetch("/api/providers")
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Provider fetch failed with ${response.status}`);
        }
        return response.json();
      })
      .then((data) => {
        const names = (Array.isArray(data?.providers) ? data.providers : [])
          .filter((provider: { available: boolean }) => provider.available)
          .map((provider: { name: string }) => provider.name);
        setAvailableProviders(names);
      })
      .catch(() => setAvailableProviders([effectiveInputProvider || "claude"]));
  }, [effectiveInputProvider]);

  useEffect(() => {
    let cancelled = false;
    fetchProviderModelCatalog()
      .then((catalog) => {
        if (!cancelled) {
          setProviderModelCatalog(catalog);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setProviderModelCatalog([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return {
    availableProviders,
    providerModelCatalog,
    viewingMeta,
    isSwappedTerminal,
    isAutonomousSession,
    isProxyAttached,
    canAttachViewedSession,
    canControlViewedSession,
    providerPickerDisabledReason,
    effectiveInputProvider,
    effectiveInputModel,
    effectiveInputReasoning,
    effectiveAgentName,
    effectiveBranch,
    showChatInput,
    chatInputDisabled,
    chatInputDisabledPlaceholder,
    chatInputDisabledAriaLabel,
    handleInputModeChange,
    handleResumeViewedSession,
    handleSwappedSessionProviderSelection,
    handleMainProviderSelection,
    handleReasoningChange,
  };
}

export type UseChatPageProviderStateResult = ReturnType<
  typeof useChatPageProviderState
>;
