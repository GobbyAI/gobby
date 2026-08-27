import {
  useCallback,
  useMemo,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { Settings } from "../../hooks/useSettings";
import type { CommandPaletteAction } from "../chat/CommandPalette";
import type { SettingsOverlayController } from "../settings/useSettingsOverlay";
import type { ChatMode, QueuedFile } from "../../types/chat";
import {
  ACTIVITY_PANEL_TABS,
  type ActivityTab,
} from "../activity/ActivityPanelTabs";
import { readDaemonRestartFailure, requestDaemonRestart } from "../../lib/api";

type ActiveModal = "skills" | "gobby" | null;

type SendMessage = (
  content: string,
  model?: string | null,
  files?: QueuedFile[],
  projectId?: string | null,
  injectContext?: string,
  reasoningEffort?: string | null,
  ttsEnabled?: boolean,
) => boolean;

interface UseAppCommandPaletteArgs {
  startNewChat: () => void;
  clearHistory: () => void;
  sendMessage: SendMessage;
  settings: Pick<Settings, "model" | "chatMode" | "ttsEnabled">;
  effectiveProjectId: string | null;
  currentMainReasoning: string | null;
  updateChatMode: (mode: ChatMode) => void;
  sendMode: (mode: ChatMode) => void;
  addSystemMessage: (content: string) => void;
  setActiveModal: Dispatch<SetStateAction<ActiveModal>>;
  settingsOverlay: Pick<SettingsOverlayController, "open">;
  setResumeModalOpen: Dispatch<SetStateAction<boolean>>;
  showPlanRef: RefObject<(() => void) | null>;
  openActivityTab: (tab: ActivityTab) => void;
}

export function useAppCommandPalette({
  startNewChat,
  clearHistory,
  sendMessage,
  settings,
  effectiveProjectId,
  currentMainReasoning,
  updateChatMode,
  sendMode,
  addSystemMessage,
  setActiveModal,
  settingsOverlay,
  setResumeModalOpen,
  showPlanRef,
  openActivityTab,
}: UseAppCommandPaletteArgs) {
  const compactConversation = useCallback(() => {
    sendMessage(
      "/compact",
      settings.model,
      undefined,
      effectiveProjectId,
      undefined,
      currentMainReasoning,
      settings.ttsEnabled,
    );
  }, [
    currentMainReasoning,
    effectiveProjectId,
    sendMessage,
    settings.model,
    settings.ttsEnabled,
  ]);

  const restartDaemon = useCallback(
    async (showFailureMessage: boolean) => {
      addSystemMessage("Requesting daemon restart...");
      try {
        const response = await requestDaemonRestart();
        const failure = await readDaemonRestartFailure(response);
        if (failure?.protectedRuns.length) {
          addSystemMessage(failure.message);
          const shouldForce = window.confirm(
            `${failure.message}\n\nForce restart and interrupt this work?`,
          );
          if (!shouldForce) return;

          addSystemMessage("Forcing daemon restart...");
          const forcedResponse = await requestDaemonRestart(true);
          const forcedFailure = await readDaemonRestartFailure(forcedResponse);
          if (forcedFailure) throw new Error(forcedFailure.message);
        } else if (failure) {
          throw new Error(failure.message);
        }
        addSystemMessage("Daemon restart requested; reconnecting...");
      } catch (err) {
        console.error("Restart request failed:", err);
        if (showFailureMessage) {
          const message =
            err instanceof Error && err.message
              ? err.message
              : "Failed to restart daemon";
          addSystemMessage(`Failed to restart daemon: ${message}`);
        }
      }
    },
    [addSystemMessage],
  );

  const handlePaletteSelect = useCallback(
    (item: PaletteItem) => {
      if (item.kind !== "command") return;

      if (item.action === "open_skills") {
        setActiveModal("skills");
        return;
      }
      if (item.action === "open_gobby") {
        setActiveModal("gobby");
        return;
      }
      if (item.action === "open_mcp") {
        openActivityTab("mcp");
        return;
      }
      if (item.action === "open_settings") {
        settingsOverlay.open();
        return;
      }
      if (item.action === "clear_history") {
        clearHistory();
        return;
      }
      if (item.action === "compact_chat") {
        compactConversation();
        return;
      }
      if (item.action === "resume_session") {
        setResumeModalOpen(true);
        return;
      }
      if (item.action === "restart_daemon") {
        void restartDaemon(false);
        return;
      }
      if (item.action === "exit_plan_mode") {
        if (settings.chatMode === "plan") {
          // Exiting plan mode from the palette lands in Act (normal); YOLO is a
          // deliberate per-approval choice on the plan card, not a default here.
          updateChatMode("normal");
          sendMode("normal");
        }
        return;
      }
      if (item.action === "show_plan") {
        if (settings.chatMode !== "plan") {
          updateChatMode("plan");
          sendMode("plan");
        }
        showPlanRef.current?.();
      }
    },
    [
      clearHistory,
      compactConversation,
      restartDaemon,
      sendMode,
      openActivityTab,
      setActiveModal,
      setResumeModalOpen,
      settingsOverlay,
      settings.chatMode,
      showPlanRef,
      updateChatMode,
    ],
  );

  const commandPaletteActions = useMemo<CommandPaletteAction[]>(() => {
    const navigationActions = ACTIVITY_PANEL_TABS.map<CommandPaletteAction>(
      ({ id, label }) => ({
        id: `nav-${id}`,
        label,
        category: "navigate",
        onSelect: () => openActivityTab(id),
      }),
    );
    const actions: CommandPaletteAction[] = [
      ...navigationActions,
      {
        id: "new-chat",
        label: "New Chat",
        icon: "+",
        category: "action",
        onSelect: () => startNewChat(),
      },
      {
        id: "resume",
        label: "Resume Session",
        icon: "\u21BA",
        category: "action",
        onSelect: () => setResumeModalOpen(true),
      },
      {
        id: "settings",
        label: "Settings",
        icon: "\u2699",
        category: "action",
        onSelect: () => settingsOverlay.open(),
      },
      {
        id: "clear",
        label: "Clear History",
        icon: "\u2715",
        category: "action",
        onSelect: () => clearHistory(),
      },
      {
        id: "compact",
        label: "Compact Conversation",
        icon: "\u2026",
        category: "action",
        onSelect: compactConversation,
      },
      {
        id: "restart",
        label: "Restart Daemon",
        icon: "\u21BB",
        category: "action",
        onSelect: () => {
          void restartDaemon(true);
        },
      },
    ];
    return actions;
  }, [
    clearHistory,
    compactConversation,
    openActivityTab,
    restartDaemon,
    setResumeModalOpen,
    settingsOverlay,
    startNewChat,
  ]);

  return { handlePaletteSelect, commandPaletteActions };
}
