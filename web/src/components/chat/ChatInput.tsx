import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import type {
  QueuedFile,
  ChatMode,
  ChatModeInfo,
  ChatSendOptions,
} from '../../types/chat'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import type { VoiceInputMode } from '../../hooks/useSettings'
import { ChatCommandPalette } from './ChatCommandPalette'
import { ChatInputModelControls } from './ChatInputModelControls'
import { ChatInputPrimaryButton } from './ChatInputPrimaryButton'
import { ChatInputQueuedFiles } from './ChatInputQueuedFiles'
import { ChatInputToolbar } from './ChatInputToolbar'
import { useChatInputAttachments } from './useChatInputAttachments'
import { useChatInputNarrow } from './useChatInputNarrow'
import { useChatInputPrimaryAction } from './useChatInputPrimaryAction'
import { useChatInputProviderSelection } from './useChatInputProviderSelection'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import {
  AUTO_REASONING_EFFORT,
  type ProviderModelEntry,
} from '../../lib/providerModels'

interface ChatInputProps {
  onSend: (
    message: string,
    files?: QueuedFile[],
    options?: ChatSendOptions,
  ) => void
  onStop?: () => void
  isStreaming?: boolean
  disabled?: boolean
  disabledPlaceholder?: string
  disabledAriaLabel?: string
  viewingSession?: boolean
  onInputChange?: (value: string) => void
  paletteItems?: PaletteItem[]
  onPaletteSelect?: (item: PaletteItem) => void
  mode?: ChatMode
  onModeChange?: (mode: ChatMode) => void
  modeOptions?: ChatModeInfo[]
  modeDisabled?: boolean
  sttEnabled?: boolean
  ttsEnabled?: boolean
  voiceInputMode?: VoiceInputMode
  isRecording?: boolean
  isSpeaking?: boolean
  voiceLoading?: boolean
  voiceReady?: boolean
  prepareTTSPlayback?: () => void
  startRecording?: () => Promise<void>
  stopRecording?: () => Promise<void>
  cancelRecording?: () => void
  stopTTS?: () => void
  onSttEnabledChange?: (enabled: boolean) => void
  onTtsEnabledChange?: (enabled: boolean) => void
  onVoiceInputModeChange?: (mode: VoiceInputMode) => void
  currentBranch?: string | null
  worktreePath?: string | null
  projectId?: string | null
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void
  worktreePickerDisabled?: boolean
  agentName?: string
  onAgentChange?: (agentName: string) => void
  agentPickerDisabled?: boolean
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
  isMobile?: boolean
  onScrollToBottom?: () => void
  provider?: string | null
  availableProviders?: string[]
  providerModelCatalog?: ProviderModelEntry[]
  currentModel?: string
  currentReasoning?: string
  onModelChange?: (model: string) => void
  onReasoningChange?: (effort: string) => void
  onProviderChange?: (provider: string | null) => void
  onSwitchProvider?: (
    provider: string,
    options?: { model?: string | null; reasoningEffort?: string | null },
  ) => void
  hasMessages?: boolean
  onProviderSelectionChange?: (
    provider: string,
    model: string,
    reasoningEffort: string | null,
  ) => void
  providerPickerDisabledReason?: string | null
  proxySlashMode?: boolean
  showObserveOverlay?: boolean
  onAttachObservedSession?: () => void
  proxyDeliveryNotice?: string | null
  attachmentsDisabled?: boolean
}

const LOCAL_ONLY_SLASH_COMMANDS = new Set([
  'gobby',
  'mcp',
  'panel',
  'restart',
  'settings',
  'skills',
])

function shouldHandleSlashCommandLocally(input: string): boolean {
  if (!input.startsWith('/')) return false
  const commandToken = input.slice(1).split(/\s/)[0] || ''
  const topLevelCommand = commandToken.split(':')[0] || commandToken
  return LOCAL_ONLY_SLASH_COMMANDS.has(topLevelCommand)
}

let fieldSizingSupport: boolean | null = null
// Chromium auto-sizes a textarea with `field-sizing: content`, so the composer
// needs no JS layout reads at all there. Cache the one-time capability check.
function supportsFieldSizing(): boolean {
  if (fieldSizingSupport === null) {
    fieldSizingSupport =
      typeof CSS !== 'undefined' &&
      typeof CSS.supports === 'function' &&
      CSS.supports('field-sizing', 'content')
  }
  return fieldSizingSupport
}

const MAX_TEXTAREA_HEIGHT = 200

export function ChatInput({
  onSend,
  onStop,
  isStreaming = false,
  disabled = false,
  disabledPlaceholder,
  disabledAriaLabel,
  viewingSession = false,
  onInputChange,
  paletteItems = [],
  onPaletteSelect,
  mode = 'normal',
  onModeChange,
  modeOptions,
  modeDisabled = false,
  sttEnabled = false,
  ttsEnabled = false,
  voiceInputMode = 'ptt',
  isRecording = false,
  isSpeaking = false,
  voiceLoading = false,
  voiceReady = false,
  prepareTTSPlayback,
  startRecording,
  stopRecording,
  cancelRecording,
  stopTTS,
  onSttEnabledChange,
  onTtsEnabledChange,
  onVoiceInputModeChange,
  currentBranch,
  worktreePath,
  projectId,
  onWorktreeChange,
  worktreePickerDisabled = false,
  agentName,
  onAgentChange,
  agentPickerDisabled = false,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
  isMobile = false,
  onScrollToBottom,
  provider,
  availableProviders = [],
  providerModelCatalog = [],
  currentModel = 'opus',
  currentReasoning = AUTO_REASONING_EFFORT,
  onModelChange,
  onReasoningChange,
  onProviderChange,
  onSwitchProvider,
  onProviderSelectionChange,
  providerPickerDisabledReason = null,
  proxySlashMode = false,
  showObserveOverlay = false,
  onAttachObservedSession,
  proxyDeliveryNotice = null,
  attachmentsDisabled = false,
}: ChatInputProps) {
  const [input, setInput] = useState('')
  const [isDragOver, setIsDragOver] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState(0)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<HTMLDivElement>(null)
  const metaRef = useRef<HTMLDivElement>(null)
  const isNarrow = useChatInputNarrow(metaRef)
  const {
    clearQueuedFiles,
    handleFilesSelected,
    hasPendingUploads,
    hasUploadErrors,
    queuedFiles,
    removeFile,
    retryFile,
  } = useChatInputAttachments({ attachmentsDisabled, projectId })

  const showPalette = input.startsWith('/') && paletteItems.length > 0

  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    // Chromium can size the textarea with CSS `field-sizing`; other browsers
    // use a rAF resize pass against the actual textarea node.
    if (supportsFieldSizing()) return
    const frame = requestAnimationFrame(() => {
      textarea.style.height = 'auto'
      const scrollHeight = textarea.scrollHeight
      const next = Math.min(scrollHeight, MAX_TEXTAREA_HEIGHT)
      textarea.style.height = `${next}px`
      if (scrollHeight > MAX_TEXTAREA_HEIGHT) {
        textarea.scrollTop = textarea.scrollHeight
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [input])

  useEffect(() => { setSelectedIndex(0) }, [paletteItems])

  // Scroll selected command into view when navigating with arrow keys
  useEffect(() => {
    const list = paletteRef.current
    if (!list) return
    const selected = list.children[selectedIndex] as HTMLElement | undefined
    selected?.scrollIntoView({ block: 'nearest' })
  }, [selectedIndex])

  const handleSubmit = useCallback(() => {
    const trimmed = input.trim()
    const filesToSend = attachmentsDisabled ? [] : queuedFiles
    const hasBlockingUpload = filesToSend.some((qf) => qf.status !== 'uploaded')
    if (hasBlockingUpload) return
    const hasFiles = filesToSend.length > 0
    if ((trimmed || hasFiles) && !disabled) {
      if (ttsEnabled) {
        prepareTTSPlayback?.()
      }
      onSend(trimmed, hasFiles ? filesToSend : undefined, {
        reasoningEffort: currentReasoning,
        ttsEnabled,
      })
      setInput('')
      clearQueuedFiles()
      onScrollToBottom?.()
    }
  }, [
    attachmentsDisabled,
    clearQueuedFiles,
    currentReasoning,
    disabled,
    input,
    onScrollToBottom,
    onSend,
    prepareTTSPlayback,
    queuedFiles,
    ttsEnabled,
  ])

  const handleChange = useCallback((value: string) => {
    setInput(value)
    onInputChange?.(value)
  }, [onInputChange])

  const handlePaletteSelect = useCallback((item: PaletteItem) => {
    if (item.kind === 'command') {
      if (item.action === 'acp_prompt') {
        if (disabled) return
        const filesToSend = attachmentsDisabled ? [] : queuedFiles
        const hasBlockingUpload = filesToSend.some((qf) => qf.status !== 'uploaded')
        if (hasBlockingUpload) return
        const commandText = item.name.startsWith('/') ? item.name : `/${item.name}`
        const trimmed = input.trim()
        const firstToken = trimmed.split(/\s/)[0]
        const prompt = firstToken === commandText ? trimmed : commandText
        if (!prompt) return
        if (ttsEnabled) {
          prepareTTSPlayback?.()
        }
        onSend(prompt, filesToSend.length > 0 ? filesToSend : undefined, {
          reasoningEffort: currentReasoning,
          ttsEnabled,
        })
        setInput('')
        clearQueuedFiles()
        onScrollToBottom?.()
        return
      }
      onPaletteSelect?.(item)
      setInput('')
    } else {
      const completed = `/${item.parentCommand}:${item.name} `
      setInput(completed)
      onInputChange?.(completed)
      textareaRef.current?.focus()
    }
  }, [
    attachmentsDisabled,
    clearQueuedFiles,
    currentReasoning,
    disabled,
    input,
    onInputChange,
    onPaletteSelect,
    onScrollToBottom,
    onSend,
    prepareTTSPlayback,
    queuedFiles,
    ttsEnabled,
  ])

  const handleKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Escape') {
      if (showPalette) { e.preventDefault(); setInput(''); return }
      if (isStreaming && onStop) { e.preventDefault(); onStop(); return }
    }
    if (showPalette) {
      if (e.key === 'ArrowUp') { e.preventDefault(); setSelectedIndex((i) => (i > 0 ? i - 1 : paletteItems.length - 1)); return }
      if (e.key === 'ArrowDown') { e.preventDefault(); setSelectedIndex((i) => (i < paletteItems.length - 1 ? i + 1 : 0)); return }
      if (e.key === 'Tab') {
        e.preventDefault()
        const selected = paletteItems[selectedIndex]
        if (selected) handlePaletteSelect(selected)
        return
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        if (!(proxySlashMode && !shouldHandleSlashCommandLocally(input))) {
          e.preventDefault()
          const selected = paletteItems[selectedIndex]
          if (selected) {
            if (selected.kind === 'sub_item') {
              // Complete the name, don't send
              handlePaletteSelect(selected)
            } else {
              handlePaletteSelect(selected)
            }
          }
          return
        }
      }
    }
    if (isMobile) {
      if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); handleSubmit() }
    } else {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit() }
    }
  }, [handleSubmit, input, isStreaming, onStop, proxySlashMode, showPalette, paletteItems, selectedIndex, handlePaletteSelect, isMobile])

  const hasInput = input.trim().length > 0 || queuedFiles.length > 0
  const pttEnabled = sttEnabled && voiceInputMode === 'ptt'
  const {
    canSelectModel,
    effectiveProvider,
    handleModelSelect,
    handleProviderSelect,
    handleReasoningSelect,
    modelOptions,
    orderedProviders,
    reasoningOptions,
    resolvedModelLabel,
    resolvedModelValue,
    resolvedReasoning,
    selectionDisabled,
  } = useChatInputProviderSelection({
    availableProviders,
    currentModel,
    currentReasoning,
    disabled,
    onModelChange,
    onProviderChange,
    onProviderSelectionChange,
    onReasoningChange,
    onSwitchProvider,
    provider,
    providerModelCatalog,
    providerPickerDisabledReason,
  })

  const {
    handleMicPointerCancel,
    handleMicKeyDown,
    handleMicKeyUp,
    handleMicPointerDown,
    handleMicPointerMove,
    handleMicPointerUp,
    handlePrimaryButtonClick,
    primaryButtonClassName,
    primaryButtonDisabled,
    primaryButtonKind,
    primaryButtonLabel,
    primaryButtonRef,
  } = useChatInputPrimaryAction({
    cancelRecording,
    disabled,
    hasInput,
    hasPendingUploads,
    hasUploadErrors,
    isRecording,
    isStreaming,
    onStop,
    onSubmit: handleSubmit,
    pttEnabled,
    startRecording,
    sttEnabled,
    stopRecording,
  })

  const disabledInputPlaceholder =
    disabledPlaceholder ??
    (viewingSession
      ? 'Read-only while watching this session...'
      : 'Message input unavailable...')
  const disabledInputAriaLabel =
    disabledAriaLabel ??
    (viewingSession
      ? 'Message input — watching read only'
      : 'Message input — unavailable')

  return (
    <div
      className={`chat-input-footer border-t border-border bg-background py-3${isDragOver ? ' ring-2 ring-accent ring-inset bg-accent/5' : ''}`}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes('application/x-gobby-file')) {
          e.preventDefault()
          e.dataTransfer.dropEffect = 'copy'
          setIsDragOver(true)
        }
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) {
          setIsDragOver(false)
        }
      }}
      onDrop={(e) => {
        const filePath = e.dataTransfer.getData('application/x-gobby-file')
        if (filePath) {
          e.preventDefault()
          setInput((prev) => prev ? `${prev} ${filePath}` : filePath)
          textareaRef.current?.focus()
        }
        setIsDragOver(false)
      }}
    >
      <div className="max-w-3xl mx-auto relative">
        {showPalette && (
          <ChatCommandPalette
            items={paletteItems}
            selectedIndex={selectedIndex}
            onSelect={handlePaletteSelect}
            paletteRef={paletteRef}
          />
        )}

        <ChatInputQueuedFiles files={queuedFiles} onRemove={removeFile} onRetry={retryFile} />

        <ChatInputToolbar
          agentDefinitions={agentDefinitions}
          agentGlobalDefs={agentGlobalDefs}
          agentHasGlobal={agentHasGlobal}
          agentHasProject={agentHasProject}
          agentName={agentName}
          agentPickerDisabled={agentPickerDisabled}
          agentProjectDefs={agentProjectDefs}
          agentShowScopeToggle={agentShowScopeToggle}
          attachmentsDisabled={attachmentsDisabled}
          canSelectModel={canSelectModel}
          currentBranch={currentBranch}
          disabled={disabled}
          fileInputRef={fileInputRef}
          handleFilesSelected={handleFilesSelected}
          isNarrow={isNarrow}
          isRecording={isRecording}
          isSpeaking={isSpeaking}
          metaRef={metaRef}
          mode={mode}
          modeDisabled={modeDisabled}
          modeOptions={modeOptions}
          onAgentChange={onAgentChange}
          onModeChange={onModeChange}
          onSttEnabledChange={onSttEnabledChange}
          onTtsEnabledChange={onTtsEnabledChange}
          onVoiceInputModeChange={onVoiceInputModeChange}
          onWorktreeChange={onWorktreeChange}
          prepareTTSPlayback={prepareTTSPlayback}
          projectId={projectId}
          proxyDeliveryNotice={proxyDeliveryNotice}
          stopTTS={stopTTS}
          sttEnabled={sttEnabled}
          ttsEnabled={ttsEnabled}
          voiceInputMode={voiceInputMode}
          voiceLoading={voiceLoading}
          voiceReady={voiceReady}
          worktreePath={worktreePath}
          worktreePickerDisabled={worktreePickerDisabled}
        />

        {/* Input row */}
        <div className="chat-input-shell">
          <div className="flex items-start gap-2">
            <textarea
              ref={textareaRef}
              name="message"
              style={{ minHeight: 'var(--control-row-height)' }}
              className="chat-input-textarea flex-1 bg-muted rounded-lg px-3 py-2 text-sm leading-5 text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-accent min-h-[36px]"
              value={input}
              onChange={(e) => handleChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                disabled
                  ? disabledInputPlaceholder
                  : isStreaming
                    ? 'Interrupt...'
                    : 'Message or /command...'
              }
              aria-label={
                disabled
                  ? disabledInputAriaLabel
                  : isStreaming
                    ? 'Message input — streaming'
                    : 'Message input'
              }
              disabled={disabled}
              rows={2}
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              inputMode="text"
              spellCheck={false}
              data-form-type="other"
              data-lpignore="true"
              data-1p-ignore
            />

            <div className="shrink-0 self-end">
              <ChatInputPrimaryButton
                buttonRef={primaryButtonRef}
                className={primaryButtonClassName}
                disabled={primaryButtonDisabled}
                kind={primaryButtonKind}
                label={primaryButtonLabel}
                onClick={handlePrimaryButtonClick}
                onMicKeyDown={handleMicKeyDown}
                onMicKeyUp={handleMicKeyUp}
                onMicPointerCancel={handleMicPointerCancel}
                onMicPointerDown={handleMicPointerDown}
                onMicPointerMove={handleMicPointerMove}
                onMicPointerUp={handleMicPointerUp}
              />
            </div>
          </div>

          {canSelectModel && (
            <ChatInputModelControls
                compact={isNarrow}
                currentBranch={currentBranch}
                disabled={disabled}
                effectiveProvider={effectiveProvider}
                hideBranch={false}
                modelOptions={modelOptions}
                onModelSelect={handleModelSelect}
                onProviderSelect={handleProviderSelect}
                onReasoningSelect={handleReasoningSelect}
                onWorktreeChange={onWorktreeChange}
                orderedProviders={orderedProviders}
                projectId={projectId}
                providerPickerDisabledReason={providerPickerDisabledReason}
                reasoningOptions={reasoningOptions}
                resolvedModelLabel={resolvedModelLabel}
                resolvedModelValue={resolvedModelValue}
                resolvedReasoning={resolvedReasoning}
                selectionDisabled={selectionDisabled}
                worktreePath={worktreePath}
                worktreePickerDisabled={worktreePickerDisabled}
              />
          )}
          {showObserveOverlay && (
            <div className="chat-input-overlay">
              <button
                type="button"
                className="chat-input-overlay__button"
                onClick={onAttachObservedSession}
              >
                Attach
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
