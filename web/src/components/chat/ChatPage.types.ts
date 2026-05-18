import type { MutableRefObject } from "react";

import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { VoiceInputMode } from "../../hooks/useSettings";
import type { ChatState, ConversationState, VoiceProps } from "../../types/chat";
import type { GobbySession } from "../../types/sessions";
import type { SessionsFilters } from "../activity/sessionsFilters";
import type { CommandPaletteAction } from "./CommandPalette";

export interface ChatPageProps {
  chat: ChatState;
  conversations: ConversationState;
  voice: VoiceProps;
  projectId?: string | null;
  showPlanRef?: MutableRefObject<(() => void) | null>;
  agentDefinitions?: AgentDefInfo[];
  agentGlobalDefs?: AgentDefInfo[];
  agentProjectDefs?: AgentDefInfo[];
  agentShowScopeToggle?: boolean;
  agentHasGlobal?: boolean;
  agentHasProject?: boolean;
  currentModel?: string;
  onModelChange?: (model: string) => void;
  reasoningPreferences?: Record<string, string>;
  onReasoningPreferenceChange?: (
    provider: string,
    model: string,
    reasoningEffort: string,
  ) => void;
  paletteActions?: CommandPaletteAction[];
  allProjectSessions?: GobbySession[];
  allProjectSessionsLoading?: boolean;
  activitySessions?: GobbySession[];
  activitySessionsLoading?: boolean;
  sessionsFilters?: SessionsFilters;
  onSessionsFiltersChange?: (filters: SessionsFilters) => void;
  onSttEnabledChange?: (enabled: boolean) => void;
  onTtsEnabledChange?: (enabled: boolean) => void;
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void;
}

export type ChatPagePaletteSelect = (item: PaletteItem) => void;
