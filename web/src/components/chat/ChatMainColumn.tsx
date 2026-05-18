import type { RefObject } from "react";

import { AUTONOMOUS_CHAT_MODES } from "../../types/chat";
import type { ChatState, VoiceProps } from "../../types/chat";
import type { ArtifactType } from "../../types/artifacts";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import type { VoiceInputMode } from "../../hooks/useSettings";
import { ArtifactContext } from "./artifacts/ArtifactContext";
import { AgentStatusBar } from "./AgentStatusBar";
import { ChatInput } from "./ChatInput";
import { CommandBar } from "./CommandBar";
import { MessageList, type MessageListHandle } from "./MessageList";
import { VoiceStatusBar } from "./VoiceStatusBar";
import type { ChatPagePaletteSelect } from "./ChatPage.types";
import type { UseChatPageProviderStateResult } from "./useChatPageProviderState";
import type { UseChatPageVoiceStatusResult } from "./useChatPageVoiceStatus";

interface AgentPickerProps {
  agentDefinitions: AgentDefInfo[];
  agentGlobalDefs: AgentDefInfo[];
  agentProjectDefs: AgentDefInfo[];
  agentShowScopeToggle: boolean;
  agentHasGlobal: boolean;
  agentHasProject: boolean;
}

interface ChatMainColumnProps extends AgentPickerProps {
  chat: ChatState;
  voice: VoiceProps;
  projectId?: string | null;
  isMobile: boolean;
  panelVisible: boolean;
  effectiveSessionRef: string | null;
  activeTitle: string | null;
  mainSessionSource: string | null;
  messageListRef: RefObject<MessageListHandle>;
  providerState: UseChatPageProviderStateResult;
  voiceStatus: UseChatPageVoiceStatusResult;
  onOpenPalette: () => void;
  onTogglePanel: () => void;
  onPaletteSelect: ChatPagePaletteSelect;
  onNewChat: (agentName?: string) => void;
  onModelChange?: (model: string) => void;
  onSttEnabledChange?: (enabled: boolean) => void;
  onTtsEnabledChange?: (enabled: boolean) => void;
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void;
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
}

export function ChatMainColumn({
  chat,
  voice,
  projectId,
  isMobile,
  panelVisible,
  effectiveSessionRef,
  activeTitle,
  mainSessionSource,
  messageListRef,
  providerState,
  voiceStatus,
  onOpenPalette,
  onTogglePanel,
  onPaletteSelect,
  onNewChat,
  onModelChange,
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
  openCodeAsArtifact,
  openFileAsArtifact,
  agentDefinitions,
  agentGlobalDefs,
  agentProjectDefs,
  agentShowScopeToggle,
  agentHasGlobal,
  agentHasProject,
}: ChatMainColumnProps) {
  const {
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
  } = providerState;

  return (
    <div className="chat-column flex flex-col flex-1 min-w-[320px]">
      <CommandBar
        sessionRef={effectiveSessionRef}
        title={viewingMeta?.title ?? activeTitle}
        sessionSource={viewingMeta?.source ?? mainSessionSource}
        onOpenPalette={onOpenPalette}
        onTogglePanel={onTogglePanel}
        panelVisible={panelVisible}
        agentDefinitions={agentDefinitions}
        agentGlobalDefs={agentGlobalDefs}
        agentProjectDefs={agentProjectDefs}
        agentShowScopeToggle={agentShowScopeToggle}
        agentHasGlobal={agentHasGlobal}
        agentHasProject={agentHasProject}
      />
      <ArtifactContext.Provider
        value={{ openCodeAsArtifact, openFileAsArtifact }}
      >
        {chat.isReconnecting && (
          <div className="bg-warning/20 text-warning-foreground text-xs text-center py-1 shrink-0">
            Reconnecting...
          </div>
        )}

        <div className="grid flex-1 min-h-0">
          <MessageList
            ref={messageListRef}
            messages={chat.messages}
            isStreaming={chat.isStreaming}
            isThinking={chat.isThinking}
            isLoadingMessages={chat.isLoadingMessages}
            onRespondToQuestion={chat.onRespondToQuestion}
            onRespondToApproval={chat.onRespondToApproval}
            canvasSurfaces={chat.canvasSurfaces}
            onCanvasInteraction={chat.onCanvasInteraction}
          />
        </div>

        {voiceStatus.showVoiceStatusBar && (
          <VoiceStatusBar
            voiceLoading={voiceStatus.voiceStatusWarming}
            isListening={voice.isListening ?? false}
            isSpeechDetected={voice.isSpeechDetected ?? false}
            isRecording={voice.isRecording ?? false}
            isTranscribing={voice.isTranscribing ?? false}
            voiceInputMode={voiceStatus.voiceInputMode}
            voiceError={voice.voiceError}
          />
        )}

        <AgentStatusBar
          viewingMeta={viewingMeta}
          interactionMode={chat.sessionInteractionMode ?? "none"}
          contextUsage={chat.contextUsage}
          contextUsageUpdatedAt={chat.contextUsageUpdatedAt}
          isAttached={!!chat.attachedSessionId}
          isAutonomousSession={isAutonomousSession}
          onAttach={canAttachViewedSession ? chat.onAttachToViewed : undefined}
          onResume={
            canControlViewedSession ? handleResumeViewedSession : undefined
          }
          onDetach={chat.attachedSessionId ? chat.onDetachFromSession : undefined}
          onNewChat={() => onNewChat()}
        />

        {showChatInput && (
          <ChatInput
            onSend={chat.onSend}
            onStop={chat.onStop}
            isStreaming={chat.isStreaming}
            disabled={chatInputDisabled}
            disabledPlaceholder={chatInputDisabledPlaceholder}
            disabledAriaLabel={chatInputDisabledAriaLabel}
            onInputChange={chat.onInputChange}
            paletteItems={chat.paletteItems}
            onPaletteSelect={onPaletteSelect}
            mode={chat.mode}
            onModeChange={handleInputModeChange}
            modeDisabled={isAutonomousSession}
            modeOptions={isAutonomousSession ? AUTONOMOUS_CHAT_MODES : undefined}
            currentBranch={effectiveBranch}
            worktreePath={chat.worktreePath}
            projectId={projectId ?? null}
            onWorktreeChange={chat.onWorktreeChange}
            worktreePickerDisabled={isProxyAttached}
            agentName={effectiveAgentName}
            onAgentChange={chat.onAgentChange}
            agentPickerDisabled={isAutonomousSession}
            agentDefinitions={agentDefinitions}
            agentGlobalDefs={agentGlobalDefs}
            agentProjectDefs={agentProjectDefs}
            agentShowScopeToggle={agentShowScopeToggle}
            agentHasGlobal={agentHasGlobal}
            agentHasProject={agentHasProject}
            sttEnabled={voice.sttEnabled}
            ttsEnabled={voice.ttsEnabled}
            voiceInputMode={voice.voiceInputMode}
            isRecording={voice.isRecording}
            isSpeaking={voice.isSpeaking}
            voiceLoading={voice.voiceLoading}
            voiceReady={voice.voiceReady}
            prepareTTSPlayback={voice.prepareTTSPlayback}
            startRecording={voice.startRecording}
            stopRecording={voice.stopRecording}
            cancelRecording={voice.cancelRecording}
            stopTTS={voice.stopTTS}
            onSttEnabledChange={onSttEnabledChange}
            onTtsEnabledChange={onTtsEnabledChange}
            onVoiceInputModeChange={onVoiceInputModeChange}
            isMobile={isMobile}
            onScrollToBottom={() => messageListRef.current?.scrollToBottom()}
            provider={effectiveInputProvider}
            availableProviders={availableProviders}
            providerModelCatalog={providerModelCatalog}
            currentModel={effectiveInputModel}
            currentReasoning={effectiveInputReasoning}
            onModelChange={onModelChange}
            onReasoningChange={handleReasoningChange}
            onProviderSelectionChange={
              isSwappedTerminal
                ? handleSwappedSessionProviderSelection
                : handleMainProviderSelection
            }
            providerPickerDisabledReason={providerPickerDisabledReason}
            hasMessages={chat.messages.length > 0}
            proxySlashMode={
              isSwappedTerminal && chat.sessionInteractionMode === "proxy"
            }
            proxyDeliveryNotice={chat.proxyDeliveryNotice}
            attachmentsDisabled={isProxyAttached}
          />
        )}
      </ArtifactContext.Provider>
    </div>
  );
}
