import { useCallback, useEffect, useMemo, useState } from "react";

import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { ConversationState, SwappedSessionTarget } from "../../types/chat";
import type { GobbySession } from "../../types/sessions";
import {
  getVisibleActivitySessions,
  toSwappedSessionTarget,
} from "../activity/activitySessionVisibility";

interface UseChatPageCommandPaletteArgs {
  activitySessions?: GobbySession[];
  allProjectSessions: GobbySession[];
  activityPanelChatSessionId: string | null;
  conversations: ConversationState;
  confirm: (options: {
    title: string;
    description?: string;
    confirmLabel?: string;
    destructive?: boolean;
  }) => Promise<boolean>;
  onPaletteSelect?: (item: PaletteItem) => void;
  handleSwapSession: (target: SwappedSessionTarget) => void;
  toggleFromChat: () => void;
  toggleFromPanel: () => void;
}

export interface UseChatPageCommandPaletteResult {
  showCommandPalette: boolean;
  setShowCommandPalette: (show: boolean) => void;
  commandPaletteSessions: GobbySession[];
  activeCommandPaletteSessionId: string | null;
  handleCommandPaletteSelectSession: (session: GobbySession) => void;
  handleCommandPaletteDeleteSession: (session: GobbySession) => Promise<void>;
  handlePaletteSelect: (item: PaletteItem) => void;
}

export function useChatPageCommandPalette({
  activitySessions,
  allProjectSessions,
  activityPanelChatSessionId,
  conversations,
  confirm,
  onPaletteSelect,
  handleSwapSession,
  toggleFromChat,
  toggleFromPanel,
}: UseChatPageCommandPaletteArgs): UseChatPageCommandPaletteResult {
  const [showCommandPalette, setShowCommandPalette] = useState(false);

  const commandPaletteSessions = useMemo(
    () =>
      getVisibleActivitySessions(activitySessions ?? allProjectSessions, {
        chatSessionId: activityPanelChatSessionId,
        liveOnly: true,
      }),
    [activityPanelChatSessionId, activitySessions, allProjectSessions],
  );

  const handleCommandPaletteSelectSession = useCallback(
    (session: GobbySession) =>
      handleSwapSession(toSwappedSessionTarget(session)),
    [handleSwapSession],
  );

  const handleCommandPaletteDeleteSession = useCallback(
    async (session: GobbySession) => {
      if (session.session_type !== "web_chat") return;

      const confirmed = await confirm({
        title: "Delete session?",
        description: `This will permanently delete ${session.ref ?? session.title ?? "this session"}.`,
        confirmLabel: "Delete",
        destructive: true,
      });
      if (confirmed) {
        conversations.onDeleteSession?.(session);
      }
    },
    [confirm, conversations],
  );

  const handlePaletteSelect = useCallback(
    (item: PaletteItem) => {
      if (item.kind === "command") {
        if (item.action === "toggle_panel") {
          toggleFromChat();
          return;
        }
        if (item.action === "toggle_chat") {
          toggleFromPanel();
          return;
        }
      }
      onPaletteSelect?.(item);
    },
    [onPaletteSelect, toggleFromChat, toggleFromPanel],
  );

  useEffect(() => {
    const handler = () => setShowCommandPalette(true);
    window.addEventListener("gobby:open-command-palette", handler);
    return () =>
      window.removeEventListener("gobby:open-command-palette", handler);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "`") {
        event.preventDefault();
        toggleFromChat();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [toggleFromChat]);

  return {
    showCommandPalette,
    setShowCommandPalette,
    commandPaletteSessions,
    activeCommandPaletteSessionId:
      activityPanelChatSessionId ?? conversations.activeSessionId,
    handleCommandPaletteSelectSession,
    handleCommandPaletteDeleteSession,
    handlePaletteSelect,
  };
}
