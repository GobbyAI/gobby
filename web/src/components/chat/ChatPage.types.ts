import type { MutableRefObject } from "react";

import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import type { VoiceInputMode } from "../../hooks/useSettings";
import type { ChatState, ConversationState, VoiceProps } from "../../types/chat";
import type { GobbySession } from "../../types/sessions";
import type { SessionsFilters } from "../activity/sessionsFilters";
import type { ActivityMcpTabProps } from "../activity/ActivityMcpTab";
import type { ActivityTab } from "../activity/ActivityPanelTabs";
import type { CommandPaletteAction } from "./CommandPalette";

export interface AgentProps {
  agentDefinitions?: AgentDefInfo[];
  agentGlobalDefs?: AgentDefInfo[];
  agentProjectDefs?: AgentDefInfo[];
  agentShowScopeToggle?: boolean;
  agentHasGlobal?: boolean;
  agentHasProject?: boolean;
}

export interface SessionFilterProps {
  allProjectSessions?: GobbySession[];
  allProjectSessionsLoading?: boolean;
  activitySessions?: GobbySession[];
  activitySessionsLoading?: boolean;
  sessionsFilters?: SessionsFilters;
  onSessionsFiltersChange?: (filters: SessionsFilters) => void;
}

export interface VoiceConfigProps {
  onSttEnabledChange?: (enabled: boolean) => void;
  onTtsEnabledChange?: (enabled: boolean) => void;
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void;
}

export interface ChatPageProps
  extends AgentProps,
    SessionFilterProps,
    VoiceConfigProps {
  chat: ChatState;
  conversations: ConversationState;
  voice: VoiceProps;
  projectId?: string | null;
  showPlanRef?: MutableRefObject<(() => void) | null>;
  currentModel?: string;
  onModelChange?: (model: string) => void;
  reasoningPreferences?: Record<string, string>;
  onReasoningPreferenceChange?: (
    provider: string,
    model: string,
    reasoningEffort: string,
  ) => void;
  paletteActions?: CommandPaletteAction[];
  mcp?: ActivityMcpTabProps;
  requestedActivityTab?: ActivityTab | null;
  onActivityTabRequestHandled?: () => void;
}

export type ChatPagePaletteSelect = (item: PaletteItem) => void;
