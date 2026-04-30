import { useState } from 'react'
import type { GobbyMemory } from '../../hooks/useMemory'
import { formatRelativeTime } from '../../utils/formatTime'

interface MemoryTableProps {
  memories: GobbyMemory[]
  onSelect: (memory: GobbyMemory) => void
  onDelete: (memoryId: string) => void
  onUpdate?: (memoryId: string, params: { content?: string; importance?: number; tags?: string[] }) => void
  onEdit?: (memory: GobbyMemory) => void
  isLoading: boolean
}

function typeColor(type: string): string {
  switch (type) {
    case 'fact': return 'var(--accent)'
    case 'preference': return 'var(--color-agent)'
    case 'pattern': return 'var(--color-review)'
    case 'context': return 'var(--color-warning-foreground)'
    default: return 'var(--text-muted)'
  }
}

function importanceBar(importance: number): string {
  if (importance >= 0.9) return 'importance-critical'
  if (importance >= 0.7) return 'importance-high'
  if (importance >= 0.5) return 'importance-medium'
  return 'importance-low'
}

function isPinned(m: GobbyMemory): boolean {
  return m.importance >= 1.0
}

export function MemoryTable({
  memories,
  onSelect,
  onDelete,
  onUpdate,
  onEdit,
  isLoading,
}: MemoryTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const handlePin = (e: React.MouseEvent, m: GobbyMemory) => {
    e.stopPropagation()
    if (!onUpdate) return
    onUpdate(m.id, { importance: isPinned(m) ? 0.5 : 1.0 })
  }

  if (isLoading) {
    return <div className="memory-empty">Loading memories...</div>
  }

  if (memories.length === 0) {
    return (
      <div className="memory-empty">
        <div className="memory-empty-icon">&#x1f9e0;</div>
        <div>No memories found</div>
        <div className="memory-empty-hint">
          Memories are created during sessions and capture important facts.
        </div>
      </div>
    )
  }

  return (
    <div className="memory-list">
      {memories.map(m => {
        const pinned = isPinned(m)
        return (
          <div
            key={m.id}
            className={`memory-card ${expandedId === m.id ? 'expanded' : ''} ${pinned ? 'memory-card--pinned' : ''}`}
            onClick={() => setExpandedId(expandedId === m.id ? null : m.id)}
            role="button"
            tabIndex={0}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpandedId(expandedId === m.id ? null : m.id) } }}
          >
            <div className="memory-card-header">
              <span
                className="memory-type-badge"
                style={{ backgroundColor: typeColor(m.memory_type) }}
              >
                {m.memory_type}
              </span>
              {pinned && <span className="memory-pin-indicator" title="Pinned">{'\u{1F4CC}'}</span>}
              <div
                className={`memory-importance ${importanceBar(m.importance)}`}
                title={`Importance: ${(m.importance * 100).toFixed(0)}%`}
              >
                <div
                  className="memory-importance-fill"
                  style={{ width: `${m.importance * 100}%` }}
                />
              </div>
              <span className="memory-date">
                {formatRelativeTime(m.created_at)}
              </span>
              <div className="memory-card-quick-actions">
                {onUpdate && (
                  <button
                    className={`memory-pin-btn ${pinned ? 'memory-pin-btn--active' : ''}`}
                    onClick={e => handlePin(e, m)}
                    title={pinned ? 'Unpin memory' : 'Pin memory'}
                  >
                    {'\u{1F4CC}'}
                  </button>
                )}
                {onEdit && (
                  <button
                    className="memory-quick-edit-btn"
                    onClick={e => {
                      e.stopPropagation()
                      onEdit(m)
                    }}
                    title="Edit memory"
                  >
                    {'\u270E'}
                  </button>
                )}
              </div>
            </div>

            <div className="memory-card-content">
              {expandedId === m.id
                ? m.content
                : m.content.length > 120
                  ? m.content.slice(0, 120) + '...'
                  : m.content}
            </div>

            {m.tags && m.tags.length > 0 && (
              <div className="memory-tags">
                {m.tags.map(tag => (
                  <span key={tag} className="memory-tag">{tag}</span>
                ))}
              </div>
            )}

            {expandedId === m.id && (
              <div className="memory-card-actions">
                <span className="memory-card-id" title={m.id}>
                  {m.id.slice(0, 12)}
                </span>
                <span className="memory-card-access">
                  {m.access_count} accesses
                </span>
                <button
                  className="memory-action-btn"
                  onClick={e => {
                    e.stopPropagation()
                    onSelect(m)
                  }}
                  title="View details"
                >
                  View
                </button>
                {onEdit && (
                  <button
                    className="memory-action-btn"
                    onClick={e => {
                      e.stopPropagation()
                      onEdit(m)
                    }}
                    title="Edit memory"
                  >
                    Edit
                  </button>
                )}
                <button
                  className="memory-delete-btn"
                  onClick={e => {
                    e.stopPropagation()
                    onDelete(m.id)
                  }}
                  title="Delete memory"
                >
                  Delete
                </button>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
