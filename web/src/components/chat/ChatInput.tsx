import { useState, useCallback, useRef, useEffect, type KeyboardEvent } from 'react'
import type { QueuedFile, ChatMode, ContextUsage } from '../../types/chat'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import { cn } from '../../lib/utils'
import { Button } from './ui/Button'
import { ModeSelector } from './ModeSelector'
import { ContextUsageIndicator } from './ContextUsageIndicator'
import { BranchIndicator } from './BranchIndicator'
import { ActiveAgentIndicator } from './ActiveAgentIndicator'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'

interface ChatInputProps {
  onSend: (message: string, files?: QueuedFile[]) => void
  onStop?: () => void
  isStreaming?: boolean
  disabled?: boolean
  viewingSession?: boolean
  onInputChange?: (value: string) => void
  paletteItems?: PaletteItem[]
  onPaletteSelect?: (item: PaletteItem) => void
  mode?: ChatMode
  onModeChange?: (mode: ChatMode) => void
  voiceMode?: boolean
  voiceAvailable?: boolean
  isListening?: boolean
  isSpeechDetected?: boolean
  isTranscribing?: boolean
  voiceError?: string | null
  onToggleVoice?: () => void
  contextUsage?: ContextUsage
  currentBranch?: string | null
  worktreePath?: string | null
  projectId?: string | null
  onWorktreeChange?: (worktreePath: string, worktreeId?: string) => void
  agentName?: string
  onAgentChange?: (agentName: string) => void
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
  isMobile?: boolean
}

export function ChatInput({
  onSend,
  onStop,
  isStreaming = false,
  disabled = false,
  viewingSession = false,
  onInputChange,
  paletteItems = [],
  onPaletteSelect,
  mode = 'accept_edits',
  onModeChange,
  voiceMode = false,
  voiceAvailable = false,
  isListening = false,
  isSpeechDetected = false,
  isTranscribing = false,
  voiceError,
  onToggleVoice,
  contextUsage,
  currentBranch,
  worktreePath,
  projectId,
  onWorktreeChange,
  agentName,
  onAgentChange,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
  isMobile = false,
}: ChatInputProps) {
  const [input, setInput] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [queuedFiles, setQueuedFiles] = useState<QueuedFile[]>([])
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<HTMLDivElement>(null)

  const showPalette = input.startsWith('/') && paletteItems.length > 0

  // Revoke blob URLs on unmount to prevent memory leaks
  const queuedFilesRef = useRef(queuedFiles)
  queuedFilesRef.current = queuedFiles
  useEffect(() => {
    return () => {
      queuedFilesRef.current.forEach((qf) => {
        if (qf.previewUrl) URL.revokeObjectURL(qf.previewUrl)
      })
    }
  }, [])

  useEffect(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = '0'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
    }
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
    const hasFiles = queuedFiles.length > 0
    if ((trimmed || hasFiles) && !disabled) {
      onSend(trimmed, hasFiles ? queuedFiles : undefined)
      setInput('')
      setQueuedFiles([])
    }
  }, [input, disabled, onSend, queuedFiles])

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
    if (!files) return
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
        setQueuedFiles((prev) => [...prev, { id, file, previewUrl, base64 }])
      }
      reader.onerror = () => {
        if (previewUrl) URL.revokeObjectURL(previewUrl)
        console.error(`Failed to read file "${file.name}"`)
      }
      reader.readAsDataURL(file)
    })
  }, [])

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
    if (isMobile) {
      if (e.key === 'Enter' && e.shiftKey) { e.preventDefault(); handleSubmit() }
    } else {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit() }
    }
  }, [handleSubmit, isStreaming, onStop, showPalette, paletteItems, selectedIndex, handlePaletteSelect, isMobile])

  const hasInput = input.trim().length > 0 || queuedFiles.length > 0

  return (
    <div className="border-t border-border bg-background px-4 py-3">
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

        {/* Toolbar */}
        <div className="flex items-center gap-1 mb-2">
          {onModeChange && (
            <ModeSelector mode={mode} onModeChange={onModeChange} disabled={disabled} />
          )}
          <button
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            onClick={() => fileInputRef.current?.click()}
            disabled={disabled}
            title="Attach file"
            aria-label="Attach file"
          >
            <PaperclipIcon />
          </button>
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
            />
          )}
          {onToggleVoice && voiceAvailable && (
            <button
              className={cn('p-1.5 rounded transition-colors', voiceMode ? 'text-accent bg-accent/20' : 'text-muted-foreground hover:text-foreground hover:bg-muted')}
              onClick={onToggleVoice}
              disabled={disabled}
              title={voiceMode ? 'Disable voice mode' : 'Enable voice mode'}
            >
              <MicIcon />
            </button>
          )}
          <div className="flex items-center gap-2 ml-auto">
          {onWorktreeChange && (
            <BranchIndicator
              currentBranch={currentBranch ?? null}
              worktreePath={worktreePath ?? null}
              projectId={projectId ?? null}
              onWorktreeChange={onWorktreeChange}
            />
          )}
          <ContextUsageIndicator
            totalInputTokens={contextUsage?.totalInputTokens ?? 0}
            outputTokens={contextUsage?.outputTokens ?? 0}
            contextWindow={contextUsage?.contextWindow ?? null}
            uncachedInputTokens={contextUsage?.uncachedInputTokens ?? 0}
            cacheReadTokens={contextUsage?.cacheReadTokens ?? 0}
            cacheCreationTokens={contextUsage?.cacheCreationTokens ?? 0}
          />
          </div>
          <input ref={fileInputRef} type="file" multiple className="hidden" onChange={(e) => { handleFilesSelected(e.target.files); e.target.value = '' }} />
        </div>

        {/* File previews */}
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

        {/* Listening indicator */}
        {voiceMode && isListening && !isTranscribing && (
          <div className="flex items-center gap-2 mb-2 px-3 py-2 rounded-lg bg-accent/10">
            {isSpeechDetected ? (
              <>
                <div className="flex gap-0.5 items-end h-4">
                  {[10, 14, 8, 12].map((h, i) => (
                    <span key={i} className="w-1 bg-green-400 rounded-full animate-pulse" style={{ height: `${h}px`, animationDelay: `${i * 0.1}s` }} />
                  ))}
                </div>
                <span className="text-sm text-green-400">Listening...</span>
              </>
            ) : (
              <>
                <span className="w-2 h-2 rounded-full bg-accent animate-pulse" />
                <span className="text-sm text-muted-foreground">Ready — speak to send</span>
              </>
            )}
          </div>
        )}

        {/* Transcribing indicator */}
        {voiceMode && isTranscribing && (
          <div className="flex items-center gap-2 mb-2 px-3 py-2 rounded-lg bg-accent/10">
            <SpinnerIcon />
            <span className="text-sm text-muted-foreground">Transcribing...</span>
          </div>
        )}

        {voiceError && (
          <div className="text-sm text-destructive-foreground mb-2">{voiceError}</div>
        )}

        {/* Input row */}
        <div className="flex items-end gap-2">
          <textarea
            ref={textareaRef}
            className="flex-1 bg-muted rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-accent min-h-[36px]"
            value={input}
            onChange={(e) => handleChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={viewingSession ? 'Attach to send messages...' : disabled ? 'Connecting...' : isStreaming ? 'Interrupt...' : voiceMode ? 'Voice mode on...' : 'Message or /command...'}
            aria-label={disabled ? 'Message input — connecting' : isStreaming ? 'Message input — streaming' : voiceMode ? 'Message input — voice mode' : 'Message input'}
            disabled={disabled}
            rows={1}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-form-type="other"
            data-lpignore="true"
            data-1p-ignore
          />

          <div className="flex gap-1 shrink-0">
            <Button size="icon" variant="primary" onClick={handleSubmit} disabled={!isStreaming && (disabled || !hasInput)}>
              <SendIcon />
            </Button>
            {isStreaming && onStop && (
              <Button size="icon" variant="outline" onClick={onStop} title="Stop generating">
                <StopIcon />
              </Button>
            )}
          </div>
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

function SpinnerIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="animate-spin">
      <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="32" />
    </svg>
  )
}
