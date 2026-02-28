import { useState, useEffect, useCallback, useMemo } from 'react'

// =============================================================================
// Types
// =============================================================================

interface MemoryEntry {
  id: string
  content: string
  memory_type: string
  importance: number
  source_session_id: string | null
  created_at: string
  tags: string[]
}

// =============================================================================
// Helpers
// =============================================================================

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
  if (imp >= 0.8) return '#22c55e'
  if (imp >= 0.5) return '#3b82f6'
  if (imp >= 0.3) return '#eab308'
  return '#737373'
}

function isPinned(mem: MemoryEntry): boolean {
  return mem.importance >= 1.0
}

const TYPE_ICONS: Record<string, string> = {
  fact: '\u2139',         // ℹ
  pattern: '\u2699',      // ⚙
  preference: '\u2605',   // ★
  decision: '\u2714',     // ✔
  lesson: '\u2728',       // ✨
  insight: '\u{1F4A1}',   // 💡
}

// =============================================================================
// TaskMemories
// =============================================================================

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
      // Filter to memories created in this session
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
    // Show confirmation preview instead of saving immediately
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
      // Pinned first, then by importance
      if (isPinned(a) !== isPinned(b)) return isPinned(a) ? -1 : 1
      return b.importance - a.importance
    }),
    [memories],
  )

  if (!sessionId) return null
  if (isLoading) return <div className="task-memories-loading">Loading memories...</div>
  if (error) return <div className="task-memories-empty">{error}</div>
  if (memories.length === 0) return <div className="task-memories-empty">No memories from this session</div>

  return (
    <div className="task-memories">
      <span className="task-memories-count">{memories.length} memor{memories.length === 1 ? 'y' : 'ies'}</span>
      <div className="task-memories-list">
        {sortedMemories.map(mem => {
          const isExpanded = expandedIds.has(mem.id)
          const isEditing = editingId === mem.id
          const isConfirming = confirmingId === mem.id
          const pinned = isPinned(mem)
          const preview = mem.content.length > 100 && !isExpanded && !isEditing && !isConfirming
            ? mem.content.slice(0, 100) + '...'
            : mem.content
          const icon = TYPE_ICONS[mem.memory_type] || '\u2022'

          return (
            <div
              key={mem.id}
              className={`task-memory-item ${pinned ? 'task-memory-item--pinned' : ''} ${isConfirming ? 'task-memory-item--confirming' : ''}`}
              onClick={() => { if (!isEditing && !isConfirming) toggle(mem.id) }}
            >
              <div className="task-memory-header">
                <span className="task-memory-icon">{icon}</span>
                {pinned && <span className="task-memory-pin-badge" title="Pinned">{'\u{1F4CC}'}</span>}
                <span className="task-memory-type">{mem.memory_type}</span>
                <span
                  className="task-memory-importance"
                  style={{ color: importanceColor(mem.importance) }}
                  title={`Importance: ${(mem.importance * 100).toFixed(0)}%`}
                >
                  {'\u25CF'} {(mem.importance * 100).toFixed(0)}%
                </span>
                <span className="task-memory-date">{formatDate(mem.created_at)}</span>
                <div className="task-memory-actions">
                  <button
                    className={`task-memory-action-btn ${pinned ? 'task-memory-action-btn--active' : ''}`}
                    onClick={(e) => handlePin(e, mem)}
                    title={pinned ? 'Unpin' : 'Pin'}
                  >
                    {'\u{1F4CC}'}
                  </button>
                  <button
                    className="task-memory-action-btn"
                    onClick={(e) => startEdit(e, mem)}
                    title="Edit"
                  >
                    {'\u270E'}
                  </button>
                </div>
              </div>

              {isEditing ? (
                <div className="task-memory-edit" onClick={e => e.stopPropagation()}>
                  <textarea
                    className="task-memory-edit-textarea"
                    value={editContent}
                    onChange={e => setEditContent(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                        e.preventDefault()
                        saveEdit(mem.id)
                      }
                      if (e.key === 'Escape') cancelEdit()
                    }}
                    rows={3}
                    autoFocus
                  />
                  <div className="task-memory-edit-buttons">
                    <button className="task-memory-edit-save" onClick={() => saveEdit(mem.id)}>Review</button>
                    <button className="task-memory-edit-cancel" onClick={cancelEdit}>Cancel</button>
                    <span className="task-memory-edit-hint">{navigator.platform?.includes('Mac') ? 'Cmd' : 'Ctrl'}+Enter to review</span>
                  </div>
                </div>
              ) : isConfirming ? (
                <div className="task-memory-confirm" onClick={e => e.stopPropagation()}>
                  <div className="task-memory-confirm-label">Agent will remember:</div>
                  <div className="task-memory-confirm-preview">{editContent.trim()}</div>
                  {editContent.trim() !== mem.content && (
                    <div className="task-memory-confirm-diff">
                      <span className="task-memory-confirm-old">Was: {mem.content.length > 80 ? mem.content.slice(0, 80) + '...' : mem.content}</span>
                    </div>
                  )}
                  <div className="task-memory-edit-buttons">
                    <button className="task-memory-edit-save" onClick={() => confirmSave(mem.id)}>Confirm</button>
                    <button className="task-memory-edit-cancel" onClick={() => { setConfirmingId(null); setEditingId(mem.id) }}>Edit Again</button>
                    <button className="task-memory-edit-cancel" onClick={cancelEdit}>Discard</button>
                  </div>
                </div>
              ) : (
                <div className="task-memory-content">{preview}</div>
              )}

              {mem.tags.length > 0 && (
                <div className="task-memory-tags">
                  {mem.tags.map((tag, i) => (
                    <span key={`${tag}-${i}`} className="task-memory-tag">{tag}</span>
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
