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
import type { ChatMode, QueuedFile } from "../../types/chat";
import { requestDaemonRestart } from "../../lib/api";
import { APP_NAV_PAGES } from "./appNavigation";

type ActiveModal = "skills" | "gobby" | "mcp" | null;

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
  settings: Pick<
    Settings,
    "model" | "chatMode" | "postPlanChatMode" | "ttsEnabled"
  >;
  effectiveProjectId: string | null;
  currentMainReasoning: string | null;
  updateChatMode: (mode: ChatMode) => void;
  sendMode: (mode: ChatMode) => void;
  addSystemMessage: (content: string) => void;
  setActiveTab: Dispatch<SetStateAction<string>>;
  setActiveModal: Dispatch<SetStateAction<ActiveModal>>;
  setSettingsOpen: Dispatch<SetStateAction<boolean>>;
  setResumeModalOpen: Dispatch<SetStateAction<boolean>>;
  showPlanRef: RefObject<(() => void) | null>;
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
  setActiveTab,
  setActiveModal,
  setSettingsOpen,
  setResumeModalOpen,
  showPlanRef,
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
      addSystemMessage("Restarting daemon...");
      try {
        const response = await requestDaemonRestart();
        if (!response.ok) {
          throw new Error(`Restart failed: ${response.status}`);
        }
        addSystemMessage("Daemon restart requested; reconnecting...");
      } catch (err) {
        console.error("Restart request failed:", err);
        if (showFailureMessage) {
          addSystemMessage("Failed to restart daemon");
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
        setActiveModal("mcp");
        return;
      }
      if (item.action === "open_settings") {
        setSettingsOpen(true);
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
        restartDaemon(false);
        return;
      }
      if (item.action === "exit_plan_mode") {
        if (settings.chatMode === "plan") {
          updateChatMode(settings.postPlanChatMode);
          sendMode(settings.postPlanChatMode);
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
      setActiveModal,
      setResumeModalOpen,
      setSettingsOpen,
      settings.chatMode,
      settings.postPlanChatMode,
      showPlanRef,
      updateChatMode,
    ],
  );

  const commandPaletteActions = useMemo<CommandPaletteAction[]>(() => {
    const actions: CommandPaletteAction[] = [
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
        onSelect: () => setSettingsOpen(true),
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
        onSelect: () => restartDaemon(true),
      },
    ];
    for (const page of APP_NAV_PAGES) {
      actions.push({
        id: `nav-${page.id}`,
        label: page.label,
        icon: "\u2192",
        category: "navigate",
        onSelect: () => setActiveTab(page.id),
      });
    }
    return actions;
  }, [
    clearHistory,
    compactConversation,
    restartDaemon,
    setActiveTab,
    setResumeModalOpen,
    setSettingsOpen,
    startNewChat,
  ]);

  return { handlePaletteSelect, commandPaletteActions };
}
