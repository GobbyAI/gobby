import { useState } from 'react'
import type { GobbyMemory } from '../../hooks/useMemory'
import { formatRelativeTime } from '../../utils/formatTime'
import { cn } from '../../lib/utils'

interface MemoryTableProps {
  memories: GobbyMemory[]
  onSelect: (memory: GobbyMemory) => void
  onDelete: (memoryId: string) => void
  onUpdate?: (memoryId: string, params: { content?: string; importance?: number; tags?: string[] }) => void
  onEdit?: (memory: GobbyMemory) => void
  isLoading: boolean
}

const IMPORTANCE_FILL_BG: Record<string, string> = {
  critical: 'bg-[var(--color-error)]',
  high: 'bg-[var(--color-warning-foreground)]',
  medium: 'bg-[var(--accent)]',
  low: 'bg-[var(--text-muted)]',
}

const ACTION_BUTTON_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-0.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)]'
const DELETE_BUTTON_CLS =
  'cursor-pointer rounded border border-[var(--color-error)] bg-transparent px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--color-error)] transition-colors duration-150 hover:bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)]'
const QUICK_BUTTON_CLS =
  'inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded border-0 bg-transparent p-0 text-[length:var(--text-base)] leading-none text-[var(--text-muted)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'

const TAG_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-2xs)] text-[var(--text-secondary)]'
const TAGS_WRAP_CLS = 'mt-1.5 flex flex-wrap gap-1'
const EMPTY_WRAP_CLS = 'flex h-full flex-col items-center justify-center gap-2 p-8 text-[var(--text-muted)]'

function typeColor(type: string): string {
  switch (type) {
    case 'fact': return 'var(--accent)'
    case 'preference': return 'var(--color-agent)'
    case 'pattern': return 'var(--color-review)'
    case 'context': return 'var(--color-warning-foreground)'
    default: return 'var(--text-muted)'
  }
}

function importanceBucket(importance: number): keyof typeof IMPORTANCE_FILL_BG {
  if (importance >= 0.9) return 'critical'
  if (importance >= 0.7) return 'high'
  if (importance >= 0.5) return 'medium'
  return 'low'
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
    return <div className={EMPTY_WRAP_CLS}>Loading memories...</div>
  }

  if (memories.length === 0) {
    return (
      <div className={EMPTY_WRAP_CLS}>
        <div className="text-[length:var(--text-4xl)] opacity-50">&#x1f9e0;</div>
        <div>No memories found</div>
        <div className="text-[length:var(--text-md)] opacity-70">
          Memories are created during sessions and capture important facts.
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {memories.map(m => {
        const pinned = isPinned(m)
        const expanded = expandedId === m.id
        const fillClass = IMPORTANCE_FILL_BG[importanceBucket(m.importance)]
        return (
          <div
            key={m.id}
            className={cn(
              'cursor-pointer rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3 transition-colors duration-150 hover:border-[var(--accent)]',
              expanded && 'border-[var(--accent)]',
            )}
            onClick={() => setExpandedId(expanded ? null : m.id)}
            role="button"
            tabIndex={0}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpandedId(expanded ? null : m.id) } }}
          >
            <div className="mb-1.5 flex items-center gap-2">
              <span
                className="rounded-sm px-1.5 py-0.5 text-[length:var(--text-2xs)] font-semibold uppercase tracking-[0.03em] text-[var(--bg-primary)]"
                style={{ backgroundColor: typeColor(m.memory_type) }}
              >
                {m.memory_type}
              </span>
              {pinned && <span title="Pinned">{'\u{1F4CC}'}</span>}
              <div
                className="h-1 w-12 overflow-hidden rounded-sm bg-[var(--bg-tertiary)]"
                title={`Importance: ${(m.importance * 100).toFixed(0)}%`}
              >
                <div
                  className={cn('h-full rounded-sm', fillClass)}
                  style={{ width: `${m.importance * 100}%` }}
                />
              </div>
              <span className="ml-auto text-[length:var(--text-xs)] text-[var(--text-muted)]">
                {formatRelativeTime(m.created_at)}
              </span>
              <div className="flex shrink-0 items-center gap-1">
                {onUpdate && (
                  <button
                    type="button"
                    className={cn(QUICK_BUTTON_CLS, pinned && 'text-[var(--accent)]')}
                    onClick={e => handlePin(e, m)}
                    title={pinned ? 'Unpin memory' : 'Pin memory'}
                  >
                    {'\u{1F4CC}'}
                  </button>
                )}
                {onEdit && (
                  <button
                    type="button"
                    className={QUICK_BUTTON_CLS}
                    onClick={e => {
                      e.stopPropagation()
                      onEdit(m)
                    }}
                    title="Edit memory"
                  >
                    {'✎'}
                  </button>
                )}
              </div>
            </div>

            <div className="whitespace-pre-wrap break-words text-[length:var(--text-base)] leading-snug text-[var(--text-primary)]">
              {expanded
                ? m.content
                : m.content.length > 120
                  ? m.content.slice(0, 120) + '...'
                  : m.content}
            </div>

            {m.tags && m.tags.length > 0 && (
              <div className={TAGS_WRAP_CLS}>
                {m.tags.map(tag => (
                  <span key={tag} className={TAG_CLS}>{tag}</span>
                ))}
              </div>
            )}

            {expanded && (
              <div className="mt-2 flex items-center gap-2 border-t border-[var(--border)] pt-1.5 text-[length:var(--text-xs)] text-[var(--text-muted)]">
                <span title={m.id}>{m.id.slice(0, 12)}</span>
                <span className="ml-auto">{m.access_count} accesses</span>
                <button
                  type="button"
                  className={ACTION_BUTTON_CLS}
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
                    type="button"
                    className={ACTION_BUTTON_CLS}
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
                  type="button"
                  className={DELETE_BUTTON_CLS}
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
