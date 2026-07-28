import type { RefObject } from 'react'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import type { VoiceInputMode } from '../../hooks/useSettings'
import type { ChatMode, ChatModeInfo } from '../../types/chat'
import { Button } from '../shared/Button'
import { ActiveAgentIndicator } from './ActiveAgentIndicator'
import { BranchIndicator } from './BranchIndicator'
import { ChatInputVoiceControls } from './ChatInputVoiceControls'
import { PaperclipIcon } from './ChatInputIcons'
import { ModeSelector } from './ModeSelector'

interface ChatInputToolbarProps {
  agentDefinitions: AgentDefInfo[]
  agentGlobalDefs: AgentDefInfo[]
  agentHasGlobal: boolean
  agentHasProject: boolean
  agentName?: string
  agentPickerDisabled: boolean
  agentProjectDefs: AgentDefInfo[]
  agentShowScopeToggle: boolean
  attachmentsDisabled: boolean
  canSelectModel: boolean
  currentBranch?: string | null
  disabled: boolean
  fileInputRef: RefObject<HTMLInputElement>
  handleFilesSelected: (files: FileList | null) => void
  isRecording: boolean
  isSpeaking: boolean
  metaRef: RefObject<HTMLDivElement>
  mode: ChatMode
  modeDisabled: boolean
  modeOptions?: ChatModeInfo[]
  onAgentChange?: (agentName: string) => void
  onModeChange?: (mode: ChatMode) => void
  onSttEnabledChange?: (enabled: boolean) => void
  onTtsEnabledChange?: (enabled: boolean) => void
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void
  prepareTTSPlayback?: () => void
  projectId?: string | null
  proxyDeliveryNotice?: string | null
  stopTTS?: () => void
  sttEnabled: boolean
  ttsEnabled: boolean
  voiceInputMode: VoiceInputMode
  voiceLoading: boolean
  voiceReady: boolean
  worktreePath?: string | null
  worktreePickerDisabled: boolean
}

export function ChatInputToolbar({
  agentDefinitions,
  agentGlobalDefs,
  agentHasGlobal,
  agentHasProject,
  agentName,
  agentPickerDisabled,
  agentProjectDefs,
  agentShowScopeToggle,
  attachmentsDisabled,
  canSelectModel,
  currentBranch,
  disabled,
  fileInputRef,
  handleFilesSelected,
  isRecording,
  isSpeaking,
  metaRef,
  mode,
  modeDisabled,
  modeOptions,
  onAgentChange,
  onModeChange,
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
  onWorktreeChange,
  prepareTTSPlayback,
  projectId,
  proxyDeliveryNotice,
  stopTTS,
  sttEnabled,
  ttsEnabled,
  voiceInputMode,
  voiceLoading,
  voiceReady,
  worktreePath,
  worktreePickerDisabled,
}: ChatInputToolbarProps) {
  const attachmentButtonLabel = attachmentsDisabled
    ? 'Attached session owns attachments'
    : 'Attach file'

  return (
    <div ref={metaRef} className="chat-input-meta">
      <div
        className={`chat-input-notice-slot${proxyDeliveryNotice ? ' has-notice' : ''}`}
        aria-live="polite"
      >
        {proxyDeliveryNotice ? (
          <div className="chat-input-notice">{proxyDeliveryNotice}</div>
        ) : null}
      </div>

      <div className="chat-input-toolbar">
        <div className="chat-input-toolbar__left">
          {onModeChange && (
            <ModeSelector
              mode={mode}
              onModeChange={onModeChange}
              disabled={disabled || modeDisabled}
              modes={modeOptions}
            />
          )}
          <Button
            size="icon"
            variant="ghost"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled || attachmentsDisabled}
            title={attachmentButtonLabel}
            aria-label={attachmentButtonLabel}
          >
            <PaperclipIcon />
          </Button>
          {onAgentChange && agentName && agentDefinitions.length > 0 && (
            <ActiveAgentIndicator
              agentName={agentName}
              onAgentChange={onAgentChange}
              definitions={agentDefinitions}
              globalDefs={agentGlobalDefs}
              projectDefs={agentProjectDefs}
              showScopeToggle={agentShowScopeToggle}
              hasGlobal={agentHasGlobal}
              hasProject={agentHasProject}
              disabled={disabled || agentPickerDisabled}
            />
          )}
          <ChatInputVoiceControls
            disabled={disabled}
            sttEnabled={sttEnabled}
            ttsEnabled={ttsEnabled}
            voiceInputMode={voiceInputMode}
            isRecording={isRecording}
            isSpeaking={isSpeaking}
            voiceLoading={voiceLoading}
            voiceReady={voiceReady}
            prepareTTSPlayback={prepareTTSPlayback}
            stopTTS={stopTTS}
            onSttEnabledChange={onSttEnabledChange}
            onTtsEnabledChange={onTtsEnabledChange}
            onVoiceInputModeChange={onVoiceInputModeChange}
          />
        </div>
        {!canSelectModel && onWorktreeChange ? (
          <div className="chat-input-toolbar__right">
            <BranchIndicator
              currentBranch={currentBranch ?? null}
              worktreePath={worktreePath ?? null}
              projectId={projectId ?? null}
              onWorktreeChange={onWorktreeChange}
              disabled={disabled || worktreePickerDisabled}
            />
          </div>
        ) : null}
        <input
          ref={fileInputRef}
          type="file"
          name="chat-attachments"
          multiple
          className="hidden"
          onChange={(event) => {
            handleFilesSelected(event.target.files)
            event.target.value = ''
          }}
        />
      </div>
    </div>
  )
}
