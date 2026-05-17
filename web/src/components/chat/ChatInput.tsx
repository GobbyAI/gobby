import { useState, useCallback, useRef, useEffect, type KeyboardEvent, type PointerEvent } from 'react'
import type {
  QueuedFile,
  ChatMode,
  ChatModeInfo,
  ChatSendOptions,
} from '../../types/chat'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import type { VoiceInputMode } from '../../hooks/useSettings'
import { cn } from '../../lib/utils'
import { Button } from '../shared/Button'
import { PaperclipIcon, RecordIcon, SendIcon, StopIcon } from './ChatInputIcons'
import { ChatInputModelControls } from './ChatInputModelControls'
import { ChatInputVoiceControls } from './ChatInputVoiceControls'
import { ModeSelector } from './ModeSelector'
import { BranchIndicator } from './BranchIndicator'
import { ActiveAgentIndicator } from './ActiveAgentIndicator'
import { useChatInputProviderSelection } from './useChatInputProviderSelection'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import {
  AUTO_REASONING_EFFORT,
  type ProviderModelEntry,
} from '../../lib/providerModels'
import {
  deleteChatAttachment,
  formatAttachmentSize,
  uploadChatAttachment,
} from '../../lib/chatAttachments'

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
  const [queuedFiles, setQueuedFiles] = useState<QueuedFile[]>([])
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
  const deletedUploadedAttachmentIdsRef = useRef<Set<string>>(new Set())
  const metaRef = useRef<HTMLDivElement>(null)
  // Track the chat-column ancestor. At <=479px we stay on the 3-row layout
  // but compress: model name + branch label truncate, toolbar buttons show
  // as glyphs only, and ModeSelector shrinks to size="sm" so Mode + the 4
  // toolbar buttons all fit on one row down to the 320px floor.
  const [isNarrow, setIsNarrow] = useState(false)
  useEffect(() => {
    const el = metaRef.current?.closest('.chat-column')
    if (!el) return
    setIsNarrow(el.getBoundingClientRect().width <= 479)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(([entry]) => {
      setIsNarrow(entry.contentRect.width <= 479)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const showPalette = input.startsWith('/') && paletteItems.length > 0

  // Revoke blob URLs on unmount to prevent memory leaks
  const queuedFilesRef = useRef(queuedFiles)
  useEffect(() => {
    queuedFilesRef.current = queuedFiles
  }, [queuedFiles])
  useEffect(() => {
    attachmentsDisabledRef.current = attachmentsDisabled
  }, [attachmentsDisabled])
  const deleteUploadedAttachment = useCallback((attachmentId: string) => {
    if (deletedUploadedAttachmentIdsRef.current.has(attachmentId)) return
    deletedUploadedAttachmentIdsRef.current.add(attachmentId)
    void deleteChatAttachment(attachmentId).catch((error: unknown) => {
      console.warn('Failed to delete uploaded chat attachment', { attachmentId, error })
    })
  }, [])
  const clearQueuedFiles = useCallback((deleteUploaded = false) => {
    queuedFilesRef.current.forEach((qf) => {
      if (qf.previewUrl) URL.revokeObjectURL(qf.previewUrl)
      if (deleteUploaded && qf.attachment) deleteUploadedAttachment(qf.attachment.id)
    })
    queuedFilesRef.current = []
    setQueuedFiles([])
  }, [deleteUploadedAttachment])
  useEffect(() => {
    return () => {
      queuedFilesRef.current.forEach((qf) => {
        if (qf.previewUrl) URL.revokeObjectURL(qf.previewUrl)
      })
    }
  }, [])
  useEffect(() => {
    if (attachmentsDisabled) {
      clearQueuedFiles(true)
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
      onPaletteSelect?.(item)
      setInput('')
    } else {
      const completed = `/${item.parentCommand}:${item.name} `
      setInput(completed)
      onInputChange?.(completed)
      textareaRef.current?.focus()
    }
  }, [onPaletteSelect, onInputChange])

  const uploadQueuedFile = useCallback(async (id: string, file: File) => {
    try {
      const attachment = await uploadChatAttachment(file, {
        projectId,
        onProgress: (progress) => {
          setQueuedFiles((prev) =>
            prev.map((qf) => qf.id === id ? { ...qf, progress } : qf),
          )
        },
      })
      setQueuedFiles((prev) => {
        const stillQueued = prev.some((qf) => qf.id === id)
        if (!stillQueued || attachmentsDisabledRef.current) {
          deleteUploadedAttachment(attachment.id)
          return prev
        }
        return prev.map((qf) =>
          qf.id === id
            ? { ...qf, status: 'uploaded', progress: 1, attachment, error: null }
            : qf,
        )
      })
    } catch (error: unknown) {
      console.warn('Attachment upload failed', { id, error })
      const message = error instanceof Error ? error.message : 'Attachment upload failed'
      setQueuedFiles((prev) =>
        prev.map((qf) =>
          qf.id === id
            ? { ...qf, status: 'error', progress: null, attachment: null, error: message }
            : qf,
        ),
      )
    }
  }, [deleteUploadedAttachment, projectId])

  const handleFilesSelected = useCallback((files: FileList | null) => {
    if (!files || attachmentsDisabled) return
    Array.from(files).forEach((file) => {
      const id = crypto.randomUUID()
      const isImage = file.type.startsWith('image/')
      const previewUrl = isImage ? URL.createObjectURL(file) : null
      setQueuedFiles((prev) => [
        ...prev,
        { id, file, previewUrl, status: 'uploading', progress: null, attachment: null, error: null },
      ])
      void uploadQueuedFile(id, file)
    })
  }, [attachmentsDisabled, uploadQueuedFile])

  const removeFile = useCallback((id: string) => {
    setQueuedFiles((prev) => {
      const removed = prev.find((f) => f.id === id)
      if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl)
      if (removed?.attachment) deleteUploadedAttachment(removed.attachment.id)
      return prev.filter((f) => f.id !== id)
    })
  }, [deleteUploadedAttachment])

  const retryFile = useCallback((id: string) => {
    const queued = queuedFilesRef.current.find((qf) => qf.id === id)
    if (!queued) return
    setQueuedFiles((prev) =>
      prev.map((qf) =>
        qf.id === id
          ? { ...qf, status: 'uploading', progress: null, attachment: null, error: null }
          : qf,
      ),
    )
    void uploadQueuedFile(id, queued.file)
  }, [uploadQueuedFile])

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

  const hasPendingUploads = queuedFiles.some((qf) => qf.status === 'uploading')
  const hasUploadErrors = queuedFiles.some((qf) => qf.status === 'error')
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
    if (!sttEnabled) {
      latchedRef.current = false
      resetPTTGesture()
    }
  }, [sttEnabled, resetPTTGesture])

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
      ? !isStreaming && (disabled || !hasInput || hasPendingUploads || hasUploadErrors)
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
    'inline-flex h-[36px] w-[36px] self-end items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50',
    primaryButtonKind === 'stop'
      ? 'border border-border bg-transparent text-foreground hover:bg-muted'
      : 'bg-accent text-accent-foreground hover:bg-accent-hover',
    primaryButtonKind === 'mic-recording' && 'ring-2 ring-red-500/70 ring-offset-2 ring-offset-background animate-pulse',
  )

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
              <div key={qf.id} className="relative rounded-md border border-border overflow-hidden bg-muted max-w-[180px]">
                {qf.previewUrl ? (
                  <img src={qf.previewUrl} alt={qf.file.name} loading="lazy" decoding="async" className="w-16 h-16 object-cover" />
                ) : (
                  <div className="flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground">
                    <PaperclipIcon />
                    <span className="max-w-[100px] truncate">{qf.file.name}</span>
                  </div>
                )}
                <div className="px-2 pb-1 text-[10px] text-muted-foreground">
                  <div className="truncate">{formatAttachmentSize(qf.attachment?.size_bytes ?? qf.file.size)}</div>
                  {qf.status === 'uploading' && (
                    <div>{qf.progress === null ? 'Uploading' : `${Math.round(qf.progress * 100)}%`}</div>
                  )}
                  {qf.status === 'error' && (
                    <button
                      type="button"
                      className="text-destructive underline"
                      onClick={() => retryFile(qf.id)}
                      title={qf.error ?? 'Upload failed'}
                    >
                      Retry
                    </button>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`Remove ${qf.file.name}`}
                  className="absolute top-0 right-0 bg-[var(--surface-scrim)] rounded-bl text-foreground w-4 h-4 flex items-center justify-center text-xs"
                  onClick={() => removeFile(qf.id)}
                >
                  &times;
                </button>
              </div>
            ))}
          </div>
        )}

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
                  size={isNarrow ? 'sm' : 'md'}
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
              {onAgentChange &&
                agentName &&
                agentDefinitions.length > 0 && (
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
            <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => { handleFilesSelected(e.target.files); e.target.value = '' }} />
          </div>
        </div>

        {/* Input row */}
        <div className="chat-input-shell">
          <div className="flex items-start gap-2">
            <textarea
              ref={textareaRef}
              style={{ minHeight: 'var(--control-row-height)' }}
              className="flex-1 bg-muted rounded-lg px-3 py-2 text-sm leading-5 text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-accent min-h-[36px]"
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
                {primaryButtonKind === 'stop' ? <StopIcon /> : primaryButtonKind === 'send' ? <SendIcon /> : <RecordIcon />}
              </button>
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
