import { useState, useEffect, useCallback, useMemo } from 'react'
import { cn } from '../../lib/utils'
import { inputFocusCls } from '../shared/focusStyles'

interface MemoryEntry {
  id: string
  content: string
  memory_type: string
  importance: number
  source_session_id: string | null
  created_at: string
  tags: string[]
}

const ROOT_CLS = 'flex flex-col gap-[0.3rem]'
const STATE_TEXT_CLS = 'text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const COUNT_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const LIST_CLS = 'flex max-h-80 flex-col gap-1 overflow-y-auto'

const ITEM_CLS =
  'group flex w-full cursor-pointer flex-col gap-[0.15rem] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-[0.4rem] text-left text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--text-muted)]'
const ITEM_PINNED_CLS =
  'border-[color-mix(in_srgb,var(--color-warning-foreground)_25%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_3%,transparent)]'
const ITEM_CONFIRMING_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-info)_5%,transparent)]'

const HEADER_CLS = 'flex items-center gap-[0.3rem] text-[length:calc(var(--font-size-base)*0.65)]'
const ICON_CLS = 'text-[length:calc(var(--font-size-base)*0.75)]'
const PIN_BADGE_CLS =
  'rounded-sm bg-[var(--color-warning-soft)] px-1 text-[length:calc(var(--font-size-base)*0.55)] font-semibold uppercase text-[var(--color-warning-foreground)]'
const TYPE_CLS = 'font-[inherit] font-semibold capitalize text-[var(--text-secondary)]'
const IMPORTANCE_CLS = 'ml-auto font-[inherit] text-[length:calc(var(--font-size-base)*0.6)]'
const DATE_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)]'

const ACTIONS_CLS = 'ml-1 flex items-center gap-0.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100'
const ACTION_BTN_CLS =
  'cursor-pointer rounded-[3px] border-0 bg-transparent px-[3px] py-px text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)] opacity-60 transition-[opacity,background] duration-150 hover:bg-[var(--bg-tertiary)] hover:opacity-100'
const ACTION_BTN_ACTIVE_CLS = 'opacity-100 text-[var(--color-warning-foreground)]'

const CONTENT_CLS = 'text-[length:calc(var(--font-size-base)*0.7)] leading-[1.4] text-[var(--text-secondary)]'

const TAGS_CLS = 'mt-[0.1rem] flex flex-wrap gap-[3px]'
const TAG_CLS =
  'rounded-[3px] border border-[var(--border)] bg-[var(--bg-tertiary)] px-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)]'

const EDIT_CLS = 'flex flex-col gap-1.5'
const EDIT_TEXTAREA_CLS =
  `w-full resize-y rounded border border-[var(--accent)] bg-[var(--bg-primary)] px-2 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] leading-[1.4] text-[var(--text-primary)] focus:border-[var(--accent-hover)] ${inputFocusCls}`
const EDIT_BUTTONS_CLS = 'flex items-center gap-1.5'
const EDIT_SAVE_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-2.5 py-[3px] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--bg-primary)] hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11'
const EDIT_CANCEL_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-[3px] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-secondary)] pointer-coarse:min-h-11'
const EDIT_HINT_CLS = 'ml-auto text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)]'

const CONFIRM_CLS = 'flex flex-col gap-1.5'
const CONFIRM_LABEL_CLS =
  'text-[length:calc(var(--font-size-base)*0.65)] font-semibold uppercase tracking-[0.5px] text-[var(--accent)]'
const CONFIRM_PREVIEW_CLS =
  'rounded border border-[color-mix(in_srgb,var(--color-info)_20%,transparent)] bg-[color-mix(in_srgb,var(--color-info)_8%,transparent)] px-2 py-1.5 text-[length:calc(var(--font-size-base)*0.72)] leading-[1.4] text-[var(--text-primary)]'
const CONFIRM_DIFF_CLS = 'flex flex-col gap-0.5'
const CONFIRM_OLD_CLS = 'text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)] line-through opacity-70'

function getBaseUrl(): string {
  return ''
}

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return 'Invalid date'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function importanceColor(imp: number): string {
  if (imp >= 0.8) return 'var(--color-success-foreground)'
  if (imp >= 0.5) return 'var(--color-info)'
  if (imp >= 0.3) return 'var(--color-warning-foreground)'
  return 'var(--text-muted)'
}

function isPinned(mem: MemoryEntry): boolean {
  return mem.importance >= 1.0
}

const TYPE_ICONS: Record<string, string> = {
  fact: 'i',
  pattern: 'P',
  preference: '*',
  decision: 'D',
  lesson: 'L',
  insight: 'I',
}

interface TaskMemoriesProps {
  sessionId: string | null
}

export function TaskMemories({ sessionId }: TaskMemoriesProps) {
  const [memories, setMemories] = useState<MemoryEntry[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [confirmingId, setConfirmingId] = useState<string | null>(null)

  const fetchMemories = useCallback(async () => {
    if (!sessionId) return
    setIsLoading(true)
    setError(null)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/memories?limit=200`)
      if (!response.ok) {
        setError('Failed to load memories')
        setIsLoading(false)
        return
      }
      const data = await response.json()
      const all: MemoryEntry[] = data.memories || []
      const sessionMemories = all.filter(m => m.source_session_id === sessionId)
      setMemories(sessionMemories)
    } catch (e) {
      console.error('Failed to fetch memories:', e)
      setError('Failed to load memories')
    }
    setIsLoading(false)
  }, [sessionId])

  useEffect(() => {
    fetchMemories()
  }, [fetchMemories])

  const toggle = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleItemKeyDown = useCallback((
    e: React.KeyboardEvent,
    id: string,
    disabled: boolean,
  ) => {
    if (disabled) return
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      toggle(id)
    }
  }, [toggle])

  const updateMemory = useCallback(async (memoryId: string, params: { content?: string; importance?: number }) => {
    setError(null)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/memories/${memoryId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      if (response.ok) {
        fetchMemories()
      } else {
        setError('Failed to update memory')
      }
    } catch (e) {
      console.error('Failed to update memory:', e)
      setError('Failed to update memory')
    }
  }, [fetchMemories])

  const handlePin = useCallback((e: React.MouseEvent, mem: MemoryEntry) => {
    e.stopPropagation()
    updateMemory(mem.id, { importance: isPinned(mem) ? 0.5 : 1.0 })
  }, [updateMemory])

  const startEdit = useCallback((e: React.MouseEvent, mem: MemoryEntry) => {
    e.stopPropagation()
    setEditingId(mem.id)
    setEditContent(mem.content)
  }, [])

  const saveEdit = useCallback((memoryId: string) => {
    if (!editContent.trim()) return
    setEditingId(null)
    setConfirmingId(memoryId)
  }, [editContent])

  const confirmSave = useCallback(async (memoryId: string) => {
    if (!editContent.trim()) return
    await updateMemory(memoryId, { content: editContent.trim() })
    setConfirmingId(null)
    setEditContent('')
  }, [editContent, updateMemory])

  const cancelEdit = useCallback(() => {
    setEditingId(null)
    setConfirmingId(null)
    setEditContent('')
  }, [])

  const sortedMemories = useMemo(
    () => [...memories].sort((a, b) => {
      if (isPinned(a) !== isPinned(b)) return isPinned(a) ? -1 : 1
      return b.importance - a.importance
    }),
    [memories],
  )

  if (!sessionId) return null
  if (isLoading) return <div className={STATE_TEXT_CLS}>Loading memories...</div>
  if (error) return <div className={STATE_TEXT_CLS}>{error}</div>
  if (memories.length === 0) return <div className={STATE_TEXT_CLS}>No memories from this session</div>

  return (
    <div className={ROOT_CLS}>
      <span className={COUNT_CLS}>{memories.length} memor{memories.length === 1 ? 'y' : 'ies'}</span>
      <div className={LIST_CLS}>
        {sortedMemories.map(mem => {
          const isExpanded = expandedIds.has(mem.id)
          const isEditing = editingId === mem.id
          const isConfirming = confirmingId === mem.id
          const pinned = isPinned(mem)
          const preview = mem.content.length > 100 && !isExpanded && !isEditing && !isConfirming
            ? mem.content.slice(0, 100) + '...'
            : mem.content
          const icon = TYPE_ICONS[mem.memory_type] || '•'

          return (
            <div
              key={mem.id}
              role="button"
              tabIndex={isEditing || isConfirming ? -1 : 0}
              aria-expanded={isExpanded}
              className={cn(ITEM_CLS, pinned && ITEM_PINNED_CLS, isConfirming && ITEM_CONFIRMING_CLS)}
              onClick={() => { if (!isEditing && !isConfirming) toggle(mem.id) }}
              onKeyDown={(e) => handleItemKeyDown(e, mem.id, isEditing || isConfirming)}
            >
              <div className={HEADER_CLS}>
                <span className={ICON_CLS}>{icon}</span>
                {pinned && <span className={PIN_BADGE_CLS} title="Pinned">Pinned</span>}
                <span className={TYPE_CLS}>{mem.memory_type}</span>
                <span
                  className={IMPORTANCE_CLS}
                  style={{ color: importanceColor(mem.importance) }}
                  title={`Importance: ${(mem.importance * 100).toFixed(0)}%`}
                >
                  {(mem.importance * 100).toFixed(0)}%
                </span>
                <span className={DATE_CLS}>{formatDate(mem.created_at)}</span>
                <div className={ACTIONS_CLS}>
                  <button
                    type="button"
                    className={cn(ACTION_BTN_CLS, pinned && ACTION_BTN_ACTIVE_CLS)}
                    onClick={(e) => handlePin(e, mem)}
                    title={pinned ? 'Unpin' : 'Pin'}
                    aria-label={pinned ? 'Unpin memory' : 'Pin memory'}
                  >
                    Pin
                  </button>
                  <button
                    type="button"
                    className={ACTION_BTN_CLS}
                    onClick={(e) => startEdit(e, mem)}
                    title="Edit"
                    aria-label="Edit memory"
                  >
                    ✎
                  </button>
                </div>
              </div>

              {isEditing ? (
                <div className={EDIT_CLS} onClick={e => e.stopPropagation()}>
                  <textarea
                    className={EDIT_TEXTAREA_CLS}
                    value={editContent}
                    onChange={e => setEditContent(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault()
                        saveEdit(mem.id)
                      }
                      if (e.key === 'Escape') cancelEdit()
                    }}
                    aria-label="Memory content"
                    rows={3}
                    autoFocus
                  />
                  <div className={EDIT_BUTTONS_CLS}>
                    <button type="button" className={EDIT_SAVE_CLS} onClick={() => saveEdit(mem.id)}>Review</button>
                    <button type="button" className={EDIT_CANCEL_CLS} onClick={cancelEdit}>Cancel</button>
                    <span className={EDIT_HINT_CLS}>{navigator.platform?.includes('Mac') ? 'Cmd' : 'Ctrl'}+Enter to review</span>
                  </div>
                </div>
              ) : isConfirming ? (
                <div className={CONFIRM_CLS} onClick={e => e.stopPropagation()}>
                  <div className={CONFIRM_LABEL_CLS}>Agent will remember:</div>
                  <div className={CONFIRM_PREVIEW_CLS}>{editContent.trim()}</div>
                  {editContent.trim() !== mem.content && (
                    <div className={CONFIRM_DIFF_CLS}>
                      <span className={CONFIRM_OLD_CLS}>Was: {mem.content.length > 80 ? mem.content.slice(0, 80) + '...' : mem.content}</span>
                    </div>
                  )}
                  <div className={EDIT_BUTTONS_CLS}>
                    <button type="button" className={EDIT_SAVE_CLS} onClick={() => confirmSave(mem.id)}>Confirm</button>
                    <button type="button" className={EDIT_CANCEL_CLS} onClick={() => { setConfirmingId(null); setEditingId(mem.id) }}>Edit Again</button>
                    <button type="button" className={EDIT_CANCEL_CLS} onClick={cancelEdit}>Discard</button>
                  </div>
                </div>
              ) : (
                <div className={CONTENT_CLS}>{preview}</div>
              )}

              {mem.tags.length > 0 && (
                <div className={TAGS_CLS}>
                  {mem.tags.map((tag, i) => (
                    <span key={`${tag}-${i}`} className={TAG_CLS}>{tag}</span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
