import { useState, useCallback, useRef, useEffect, type KeyboardEvent, type PointerEvent } from 'react'
import type { QueuedFile, ChatMode, ChatModeInfo, ChatSendOptions } from '../../types/chat'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import type { VoiceInputMode } from '../../hooks/useSettings'
import { cn } from '../../lib/utils'
import { Button } from './ui/Button'
import { ModeSelector } from './ModeSelector'
import { BranchIndicator } from './BranchIndicator'
import { ActiveAgentIndicator } from './ActiveAgentIndicator'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger } from './ui/Select'
import { SourceIcon } from '../shared/SourceIcon'
import {
  AUTO_REASONING_EFFORT,
  getModelLabel,
  getModelsForProvider,
  getOrderedProviders,
  getPreferredModelForProvider,
  getPreferredReasoningEffort,
  getProviderDisplayName,
  getReasoningOptionsForModel,
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
  startRecording?: () => Promise<void>
  stopRecording?: () => Promise<void>
  cancelRecording?: () => void
  stopTTS?: () => void
  onSttEnabledChange?: (enabled: boolean) => void
  onTtsEnabledChange?: (enabled: boolean) => void
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
  onToggleActivityPanel?: () => void
  isActivityPanelPinned?: boolean
}

const LOCAL_ONLY_SLASH_COMMANDS = new Set(['settings', 'panel', 'gobby', 'mcp', 'skills'])

function shouldHandleSlashCommandLocally(input: string): boolean {
  if (!input.startsWith('/')) return false
  const commandToken = input.slice(1).split(/\s/)[0] || ''
  const topLevelCommand = commandToken.split(':')[0] || commandToken
  return LOCAL_ONLY_SLASH_COMMANDS.has(topLevelCommand)
}

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
  startRecording,
  stopRecording,
  cancelRecording,
  stopTTS,
  onSttEnabledChange,
  onTtsEnabledChange,
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
  onToggleActivityPanel,
  isActivityPanelPinned = false,
}: ChatInputProps) {
  const [input, setInput] = useState('')
  const [isDragOver, setIsDragOver] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [queuedFiles, setQueuedFiles] = useState<QueuedFile[]>([])
  const [pendingSttStart, setPendingSttStart] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<HTMLDivElement>(null)
  const primaryButtonRef = useRef<HTMLButtonElement>(null)
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const holdActiveRef = useRef(false)
  const latchedRef = useRef(false)
  const activePointerIdRef = useRef<number | null>(null)
  const pointerStartedWhileRecordingRef = useRef(false)
  const attachmentsDisabledRef = useRef(attachmentsDisabled)

  const showPalette = input.startsWith('/') && paletteItems.length > 0

  // Revoke blob URLs on unmount to prevent memory leaks
  const queuedFilesRef = useRef(queuedFiles)
  useEffect(() => {
    queuedFilesRef.current = queuedFiles
  }, [queuedFiles])
  useEffect(() => {
    attachmentsDisabledRef.current = attachmentsDisabled
  }, [attachmentsDisabled])
  const clearQueuedFiles = useCallback(() => {
    queuedFilesRef.current.forEach((qf) => {
      if (qf.previewUrl) URL.revokeObjectURL(qf.previewUrl)
    })
    queuedFilesRef.current = []
    setQueuedFiles([])
  }, [])
  useEffect(() => {
    return () => {
      queuedFilesRef.current.forEach((qf) => {
        if (qf.previewUrl) URL.revokeObjectURL(qf.previewUrl)
      })
    }
  }, [])
  useEffect(() => {
    if (attachmentsDisabled) {
      clearQueuedFiles()
    }
  }, [attachmentsDisabled, clearQueuedFiles])

  useEffect(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = '0'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
      // Keep cursor visible when content exceeds max height
      textarea.scrollTop = textarea.scrollHeight
    }
  }, [input])

  useEffect(() => { setSelectedIndex(0) }, [paletteItems])

  const clearHoldTimer = useCallback(() => {
    if (holdTimerRef.current) {
      clearTimeout(holdTimerRef.current)
      holdTimerRef.current = null
    }
  }, [])

  const resetPTTGesture = useCallback(() => {
    clearHoldTimer()
    holdActiveRef.current = false
    activePointerIdRef.current = null
    pointerStartedWhileRecordingRef.current = false
  }, [clearHoldTimer])

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
    const hasFiles = filesToSend.length > 0
    if ((trimmed || hasFiles) && !disabled) {
      onSend(trimmed, hasFiles ? filesToSend : undefined, {
        reasoningEffort: currentReasoning,
      })
      setInput('')
      clearQueuedFiles()
      onScrollToBottom?.()
    }
  }, [attachmentsDisabled, clearQueuedFiles, currentReasoning, disabled, input, onScrollToBottom, onSend, queuedFiles])

  const handleChange = useCallback((value: string) => {
    setInput(value)
    onInputChange?.(value)
  }, [onInputChange])

  const handlePaletteSelect = useCallback((item: PaletteItem) => {
    if (item.kind === 'command') {
      onPaletteSelect?.(item)
      setInput('')
    } else {
      const completed = `/${item.parentCommand}:${item.name} `
      setInput(completed)
      onInputChange?.(completed)
      textareaRef.current?.focus()
    }
  }, [onPaletteSelect, onInputChange])

  const handleFilesSelected = useCallback((files: FileList | null) => {
    if (!files || attachmentsDisabled) return
    const MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024 // 5 MB
    Array.from(files).forEach((file) => {
      if (file.size > MAX_FILE_SIZE_BYTES) {
        console.warn(`File "${file.name}" exceeds ${MAX_FILE_SIZE_BYTES / 1024 / 1024}MB limit, skipping`)
        return
      }
      const id = crypto.randomUUID()
      const isImage = file.type.startsWith('image/')
      const previewUrl = isImage ? URL.createObjectURL(file) : null
      const reader = new FileReader()
      reader.onload = () => {
        const result = reader.result as string
        const base64 = result.split(',')[1] || null
        if (attachmentsDisabledRef.current) {
          if (previewUrl) URL.revokeObjectURL(previewUrl)
          return
        }
        setQueuedFiles((prev) => [...prev, { id, file, previewUrl, base64 }])
      }
      reader.onerror = () => {
        if (previewUrl) URL.revokeObjectURL(previewUrl)
        console.error(`Failed to read file "${file.name}"`)
      }
      reader.readAsDataURL(file)
    })
  }, [attachmentsDisabled])

  const removeFile = useCallback((id: string) => {
    setQueuedFiles((prev) => {
      const removed = prev.find((f) => f.id === id)
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl)
      return prev.filter((f) => f.id !== id)
    })
  }, [])

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
  const effectiveProvider = provider ?? 'claude'
  const pickerProviders =
    availableProviders.length > 0 ? availableProviders : [effectiveProvider]
  const orderedProviders = getOrderedProviders(pickerProviders)
  const visibleModels = getModelsForProvider(providerModelCatalog, effectiveProvider)
  const modelOptions =
    visibleModels.length > 0
      ? visibleModels
      : [{ value: currentModel || 'default', label: getModelLabel(providerModelCatalog, effectiveProvider, currentModel) }]
  const resolvedModelValue = currentModel || modelOptions[0]?.value || 'default'
  const resolvedModelLabel = getModelLabel(
    providerModelCatalog,
    effectiveProvider,
    resolvedModelValue,
  )
  const reasoningOptions = getReasoningOptionsForModel(
    providerModelCatalog,
    effectiveProvider,
    resolvedModelValue,
  )
  const resolvedReasoning =
    currentReasoning ||
    getPreferredReasoningEffort(
        providerModelCatalog,
        effectiveProvider,
        resolvedModelValue,
        AUTO_REASONING_EFFORT,
      )
  const canSelectModel = Boolean(onModelChange)
  const selectionDisabled = disabled || Boolean(providerPickerDisabledReason)

  const applySelection = useCallback(
    (nextProvider: string, nextModel: string, nextReasoning: string) => {
      if (onProviderSelectionChange) {
        onProviderSelectionChange(nextProvider, nextModel, nextReasoning)
        return
      }

      const providerChanged = nextProvider !== effectiveProvider
      if (providerChanged) {
        onProviderChange?.(nextProvider)
      }
      onModelChange?.(nextModel)
      onReasoningChange?.(nextReasoning)
      if (providerChanged) {
        onSwitchProvider?.(nextProvider, {
          model: nextModel,
          reasoningEffort: nextReasoning,
        })
      }
    },
    [
      effectiveProvider,
      onModelChange,
      onProviderChange,
      onProviderSelectionChange,
      onReasoningChange,
      onSwitchProvider,
    ],
  )

  const handleProviderSelect = (nextProvider: string) => {
    const nextModel =
      getPreferredModelForProvider(providerModelCatalog, nextProvider, null) ??
      resolvedModelValue ??
      'default'
    const nextReasoning = getPreferredReasoningEffort(
      providerModelCatalog,
      nextProvider,
      nextModel,
      null,
    )
    applySelection(nextProvider, nextModel, nextReasoning)
  }

  const handleModelSelect = useCallback(
    (nextModel: string) => {
      const nextReasoning = getPreferredReasoningEffort(
        providerModelCatalog,
        effectiveProvider,
        nextModel,
        resolvedReasoning,
      )
      applySelection(effectiveProvider, nextModel, nextReasoning)
    },
    [applySelection, effectiveProvider, providerModelCatalog, resolvedReasoning],
  )

  const handleReasoningSelect = (nextReasoning: string) => {
    applySelection(effectiveProvider, resolvedModelValue, nextReasoning)
  }

  type PrimaryButtonKind = 'stop' | 'mic-idle' | 'mic-recording' | 'send'

  const resolvePrimaryButtonKind = (): PrimaryButtonKind => {
    if (isStreaming) return 'stop'
    if (pttEnabled && !hasInput) {
      return isRecording ? 'mic-recording' : 'mic-idle'
    }
    return 'send'
  }

  const primaryButtonKind = resolvePrimaryButtonKind()

  useEffect(() => {
    if (!isRecording) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [isRecording, resetPTTGesture])

  useEffect(() => {
    if (!pttEnabled) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [pttEnabled, resetPTTGesture])

  useEffect(() => {
    if (!pendingSttStart) return
    if (!sttEnabled || isRecording || !startRecording) return

    setPendingSttStart(false)
    void startRecording()
  }, [isRecording, pendingSttStart, startRecording, sttEnabled])

  useEffect(() => {
    if (!isRecording || !cancelRecording) return

    const handleWindowKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      latchedRef.current = false
      resetPTTGesture()
      cancelRecording()
    }

    window.addEventListener('keydown', handleWindowKeyDown)
    return () => window.removeEventListener('keydown', handleWindowKeyDown)
  }, [cancelRecording, isRecording, resetPTTGesture])

  useEffect(() => {
    return () => clearHoldTimer()
  }, [clearHoldTimer])

  const handleMicPointerDown = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (disabled || primaryButtonKind === 'stop' || !startRecording) return
    if (event.button !== 0) return

    event.preventDefault()
    primaryButtonRef.current?.setPointerCapture(event.pointerId)
    activePointerIdRef.current = event.pointerId
    pointerStartedWhileRecordingRef.current = Boolean(isRecording && latchedRef.current)
    holdActiveRef.current = false
    clearHoldTimer()
    holdTimerRef.current = setTimeout(() => {
      holdActiveRef.current = true
    }, 250)

    if (!isRecording) {
      void startRecording()
    }
  }, [clearHoldTimer, disabled, isRecording, primaryButtonKind, startRecording])

  const handleMicPointerUp = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (activePointerIdRef.current !== null && event.pointerId !== activePointerIdRef.current) {
      return
    }

    if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
      primaryButtonRef.current.releasePointerCapture(event.pointerId)
    }

    const wasHold = holdActiveRef.current
    const wasLatchedStopTap = pointerStartedWhileRecordingRef.current
    resetPTTGesture()

    if (wasHold || wasLatchedStopTap) {
      latchedRef.current = false
      void stopRecording?.()
      return
    }

    latchedRef.current = true
  }, [resetPTTGesture, stopRecording])

  const handleMicPointerMove = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (!holdActiveRef.current || !cancelRecording) return
    if (activePointerIdRef.current !== event.pointerId) return

    const rect = primaryButtonRef.current?.getBoundingClientRect()
    if (!rect) return

    const outside =
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom

    if (outside) {
      if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
        primaryButtonRef.current.releasePointerCapture(event.pointerId)
      }
      latchedRef.current = false
      resetPTTGesture()
      cancelRecording()
    }
  }, [cancelRecording, resetPTTGesture])

  const handleMicPointerCancel = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (primaryButtonRef.current?.hasPointerCapture(event.pointerId)) {
      primaryButtonRef.current.releasePointerCapture(event.pointerId)
    }
    latchedRef.current = false
    resetPTTGesture()
    cancelRecording?.()
  }, [cancelRecording, resetPTTGesture])

  const handlePrimaryButtonClick = useCallback(() => {
    if (primaryButtonKind === 'stop') {
      onStop?.()
      return
    }
    if (primaryButtonKind === 'send') {
      handleSubmit()
    }
  }, [handleSubmit, onStop, primaryButtonKind])

  const primaryButtonDisabled =
    primaryButtonKind === 'send'
      ? !isStreaming && (disabled || !hasInput)
      : primaryButtonKind === 'stop'
        ? false
        : disabled || !startRecording || !stopRecording || !cancelRecording

  const primaryButtonLabel =
    primaryButtonKind === 'stop'
      ? 'Stop generating'
      : primaryButtonKind === 'send'
        ? 'Send message'
        : primaryButtonKind === 'mic-recording'
          ? 'Push to talk recording'
          : 'Start push to talk'
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

  const primaryButtonClassName = cn(
    'inline-flex h-[52px] w-[52px] self-start items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50',
    primaryButtonKind === 'stop'
      ? 'border border-border bg-transparent text-foreground hover:bg-muted'
      : 'bg-accent text-accent-foreground hover:bg-accent-hover',
    primaryButtonKind === 'mic-recording' && 'ring-2 ring-red-500/70 ring-offset-2 ring-offset-background animate-pulse',
  )

  const activityPanelButtonLabel = isActivityPanelPinned
    ? 'Hide activity panel'
    : 'Show activity panel'

  return (
    <div
      className={`border-t border-border bg-background px-4 py-3${isDragOver ? ' ring-2 ring-accent ring-inset bg-accent/5' : ''}`}
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
        {/* Command palette */}
        {showPalette && (
          <div ref={paletteRef} className="command-palette font-sans">
            {paletteItems.map((item, i) => (
              <div
                key={item.kind === 'command' ? item.name : `${item.parentCommand}:${item.name}${item.serverName ? `:${item.serverName}` : ''}`}
                className={cn(
                  'px-3 py-2 text-sm cursor-pointer',
                  i === selectedIndex ? 'bg-accent/20 text-foreground' : 'text-muted-foreground hover:bg-muted'
                )}
                onClick={() => handlePaletteSelect(item)}
              >
                {item.kind === 'command' ? (
                  <>
                    <span className="font-mono">/{item.name}</span>
                    {item.description && <span className="ml-2 text-xs opacity-60">{item.description}</span>}
                  </>
                ) : (
                  <>
                    <span className="font-mono">{item.name}</span>
                    {item.serverName && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-accent/10 text-accent">{item.serverName}</span>
                    )}
                    {item.description && <span className="ml-2 text-xs opacity-60 truncate">{item.description}</span>}
                  </>
                )}
              </div>
            ))}
          </div>
        )}

        {queuedFiles.length > 0 && (
          <div className="flex gap-2 mb-2 flex-wrap">
            {queuedFiles.map((qf) => (
              <div key={qf.id} className="relative rounded-md border border-border overflow-hidden bg-muted">
                {qf.previewUrl ? (
                  <img src={qf.previewUrl} alt={qf.file.name} className="w-16 h-16 object-cover" />
                ) : (
                  <div className="flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground">
                    <PaperclipIcon />
                    <span className="max-w-[100px] truncate">{qf.file.name}</span>
                  </div>
                )}
                <button
                  className="absolute top-0 right-0 bg-black/60 rounded-bl text-foreground w-4 h-4 flex items-center justify-center text-xs"
                  onClick={() => removeFile(qf.id)}
                >
                  &times;
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="chat-input-meta">
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
                title={
                  attachmentsDisabled
                    ? 'Attached session owns attachments'
                    : 'Attach file'
                }
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
              {onToggleActivityPanel && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={onToggleActivityPanel}
                  title={activityPanelButtonLabel}
                  aria-label={activityPanelButtonLabel}
                  className={cn(
                    'chat-input-panel-toggle',
                    isActivityPanelPinned && 'chat-input-panel-toggle--active',
                  )}
                >
                  <PanelIcon pinned={isActivityPanelPinned} />
                </Button>
              )}
              {onTtsEnabledChange && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => {
                    if (isSpeaking) {
                      stopTTS?.()
                      return
                    }
                    onTtsEnabledChange(!ttsEnabled)
                  }}
                  title={
                    isSpeaking
                      ? 'Stop speaking'
                      : ttsEnabled
                        ? 'Disable text-to-speech'
                        : 'Enable text-to-speech'
                  }
                  aria-label="Toggle text-to-speech"
                  aria-pressed={ttsEnabled || isSpeaking}
                  className={cn(
                    'chat-input-voice-toggle',
                    (ttsEnabled || isSpeaking) && 'chat-input-voice-toggle--active',
                  )}
                >
                  <SpeakerIcon muted={!ttsEnabled && !isSpeaking} />
                </Button>
              )}
              {onSttEnabledChange && startRecording && stopRecording && (
                <Button
                  size="icon"
                  variant="ghost"
                  onClick={() => {
                    if (isRecording) {
                      setPendingSttStart(false)
                      void stopRecording()
                      return
                    }
                    if (!sttEnabled) {
                      setPendingSttStart(true)
                      onSttEnabledChange(true)
                      return
                    }
                    void startRecording()
                  }}
                  title={
                    isRecording
                      ? 'Stop recording'
                      : sttEnabled
                        ? 'Start recording'
                        : 'Enable speech-to-text and start recording'
                  }
                  aria-label="Toggle microphone"
                  aria-pressed={isRecording || sttEnabled}
                  className={cn(
                    'chat-input-voice-toggle',
                    (isRecording || sttEnabled) && 'chat-input-voice-toggle--active',
                  )}
                >
                  <MicIcon />
                </Button>
              )}
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
            <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => { handleFilesSelected(e.target.files); e.target.value = '' }} />
          </div>
        </div>

        {/* Input row */}
        <div className="chat-input-shell">
          <div className="flex items-start gap-2">
            <textarea
              ref={textareaRef}
              className="flex-1 bg-muted rounded-lg px-3 py-2 text-sm leading-5 text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-accent min-h-[52px]"
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

            <div className="shrink-0 self-start">
              <button
                ref={primaryButtonRef}
                type="button"
                className={primaryButtonClassName}
                onClick={primaryButtonKind === 'mic-idle' || primaryButtonKind === 'mic-recording' ? undefined : handlePrimaryButtonClick}
                onPointerDown={primaryButtonKind === 'mic-idle' || primaryButtonKind === 'mic-recording' ? handleMicPointerDown : undefined}
                onPointerUp={primaryButtonKind === 'mic-idle' || primaryButtonKind === 'mic-recording' ? handleMicPointerUp : undefined}
                onPointerMove={primaryButtonKind === 'mic-idle' || primaryButtonKind === 'mic-recording' ? handleMicPointerMove : undefined}
                onPointerCancel={primaryButtonKind === 'mic-idle' || primaryButtonKind === 'mic-recording' ? handleMicPointerCancel : undefined}
                title={primaryButtonLabel}
                aria-label={primaryButtonLabel}
                aria-pressed={primaryButtonKind === 'mic-recording' ? true : undefined}
                disabled={primaryButtonDisabled}
              >
                {primaryButtonKind === 'stop' ? <StopIcon /> : primaryButtonKind === 'send' ? <SendIcon /> : <MicIcon />}
              </button>
            </div>
          </div>

          {canSelectModel && (
            <div className="chat-input-controls">
              <div className="chat-input-model-controls">
                <Select
                  value={effectiveProvider}
                  onValueChange={handleProviderSelect}
                  disabled={selectionDisabled}
                >
                  <SelectTrigger
                    className="chat-input-select chat-input-select--provider chat-input-select--provider-icon !w-auto"
                    aria-label="Select provider"
                    title={providerPickerDisabledReason ?? getProviderDisplayName(effectiveProvider)}
                  >
                    <div className="chat-input-select__value">
                      <SourceIcon source={effectiveProvider} size={14} />
                    </div>
                  </SelectTrigger>
                  <SelectContent side="top" className="chat-input-select__content">
                    <SelectGroup>
                      <SelectLabel className="chat-input-select__label">Provider</SelectLabel>
                      {orderedProviders.map((candidateProvider) => (
                        <SelectItem key={candidateProvider} value={candidateProvider}>
                          <span className="chat-input-select__item">
                            <SourceIcon source={candidateProvider} size={14} />
                            <span>{getProviderDisplayName(candidateProvider)}</span>
                          </span>
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>

                <Select
                  value={resolvedModelValue}
                  onValueChange={handleModelSelect}
                  disabled={selectionDisabled}
                >
                  <SelectTrigger
                    className="chat-input-select chat-input-select--model !w-auto"
                    aria-label="Select model"
                    title={providerPickerDisabledReason ?? 'Select model'}
                  >
                    <div className="chat-input-select__value">
                      <span className="chat-input-select__text">
                        {resolvedModelLabel}
                      </span>
                    </div>
                  </SelectTrigger>
                  <SelectContent side="top" className="chat-input-select__content">
                    <SelectGroup>
                      <SelectLabel className="chat-input-select__label">Model</SelectLabel>
                      {modelOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>

                <Select
                  value={resolvedReasoning}
                  onValueChange={handleReasoningSelect}
                  disabled={
                    selectionDisabled ||
                    (reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled))
                  }
                >
                  <SelectTrigger
                    className="chat-input-select chat-input-select--reasoning !w-auto"
                    aria-label="Select reasoning effort"
                    title={providerPickerDisabledReason ?? 'Select reasoning effort'}
                  >
                    <div className="chat-input-select__value">
                      <BrainIcon />
                      <span className="chat-input-select__text">
                        {reasoningOptions.find((option) => option.value === resolvedReasoning)?.label ?? 'Auto'}
                      </span>
                    </div>
                  </SelectTrigger>
                  <SelectContent side="top" className="chat-input-select__content">
                    <SelectGroup>
                      <SelectLabel className="chat-input-select__label">Effort</SelectLabel>
                      {reasoningOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value} disabled={option.disabled}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>

                {onWorktreeChange && (
                  <BranchIndicator
                    currentBranch={currentBranch ?? null}
                    worktreePath={worktreePath ?? null}
                    projectId={projectId ?? null}
                    onWorktreeChange={onWorktreeChange}
                    disabled={disabled || worktreePickerDisabled}
                    variant="select"
                  />
                )}

              </div>
            </div>
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

function SendIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="2" x2="11" y2="13" />
      <polygon points="22 2 15 22 11 13 2 9 22 2" />
    </svg>
  )
}

function StopIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <rect x="3" y="3" width="10" height="10" rx="1" />
    </svg>
  )
}

function MicIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  )
}

function PaperclipIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </svg>
  )
}

function SpeakerIcon({ muted = false }: { muted?: boolean }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      {muted ? (
        <>
          <line x1="22" y1="9" x2="16" y2="15" />
          <line x1="16" y1="9" x2="22" y2="15" />
        </>
      ) : (
        <>
          <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
          <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
        </>
      )}
    </svg>
  )
}

function PanelIcon({ pinned }: { pinned: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="15" y1="3" x2="15" y2="21" />
      {pinned && <line x1="18" y1="9" x2="21" y2="9" opacity="0.5" />}
    </svg>
  )
}

function BrainIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9.5 2A3.5 3.5 0 0 0 6 5.5v1A2.5 2.5 0 0 0 3.5 9v1.5A2.5 2.5 0 0 0 6 13v3a4 4 0 0 0 4 4" />
      <path d="M14.5 2A3.5 3.5 0 0 1 18 5.5v1A2.5 2.5 0 0 1 20.5 9v1.5A2.5 2.5 0 0 1 18 13v3a4 4 0 0 1-4 4" />
      <path d="M9 8h1" />
      <path d="M14 8h1" />
      <path d="M9 12h6" />
      <path d="M12 13v5" />
    </svg>
  )
}
