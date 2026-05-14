import { useEffect } from 'react'
import type { GobbyMemory } from '../../hooks/useMemory'
import { formatRelativeTime, typeLabel } from '../../utils/formatTime'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { cn } from '../../lib/utils'
import { Heading } from '../shared/Heading'

interface MemoryDetailProps {
  memory: GobbyMemory | null
  onEdit: () => void
  onDelete: () => void
  onClose: () => void
}

const SAVE_BUTTON_CLS =
  'cursor-pointer rounded-md border border-[var(--accent)] bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-base)] font-medium text-[var(--bg-primary)] hover:opacity-90 pointer-coarse:min-h-11'
const DELETE_BUTTON_CLS =
  'cursor-pointer rounded border border-[var(--color-error)] bg-transparent px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--color-error)] transition-colors duration-150 hover:bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] pointer-coarse:min-h-11 pointer-coarse:px-3'
const TAG_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-2xs)] text-[var(--text-secondary)]'

export function MemoryDetail({ memory, onEdit, onDelete, onClose }: MemoryDetailProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const isOpen = memory !== null

  useEffect(() => {
    if (!isOpen) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  return (
    <>
      {ConfirmDialogElement}
      <div
        className={cn(
          'pointer-events-none fixed inset-0 z-[90] bg-[var(--surface-scrim)] opacity-0 transition-opacity duration-200',
          isOpen && 'pointer-events-auto opacity-100',
        )}
        onClick={onClose}
      />
      <div
        className={cn(
          'fixed bottom-0 right-0 top-0 z-[100] flex w-[420px] max-w-[90vw] translate-x-full flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-primary)] transition-transform duration-[250ms] ease-[cubic-bezier(0.4,0,0.2,1)]',
          isOpen && 'translate-x-0',
        )}
      >
        {memory && (
          <div className="flex flex-1 flex-col gap-3 px-5 py-4">
            <div className="flex items-center justify-between">
              <Heading level={3} className="m-0 text-[length:var(--text-lg)] text-[var(--text-primary)]">Memory Detail</Heading>
              <button
                type="button"
                className="flex h-8 w-8 cursor-pointer items-center justify-center border-0 bg-transparent p-1 text-[length:var(--text-2xl)] leading-none text-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11"
                onClick={onClose}
                aria-label="Close detail"
              >
                &times;
              </button>
            </div>

            <div className="whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] p-3 text-[length:var(--text-base)] leading-relaxed text-[var(--text-primary)]">
              {memory.content}
            </div>

            <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[length:var(--text-md)]">
              <div className="font-medium text-[var(--text-muted)]">Type</div>
              <div className="text-[var(--text-primary)]">{typeLabel(memory.memory_type)}</div>

              <div className="font-medium text-[var(--text-muted)]">Importance</div>
              <div className="text-[var(--text-primary)]">{(memory.importance * 100).toFixed(0)}%</div>

              <div className="font-medium text-[var(--text-muted)]">Source</div>
              <div className="text-[var(--text-primary)]">{memory.source_type ?? 'Unknown'}</div>

              <div className="font-medium text-[var(--text-muted)]">Created</div>
              <div className="text-[var(--text-primary)]">{formatRelativeTime(memory.created_at)}</div>

              <div className="font-medium text-[var(--text-muted)]">Updated</div>
              <div className="text-[var(--text-primary)]">{formatRelativeTime(memory.updated_at)}</div>

              <div className="font-medium text-[var(--text-muted)]">Access Count</div>
              <div className="text-[var(--text-primary)]">{memory.access_count}</div>

              {memory.last_accessed_at && (
                <>
                  <div className="font-medium text-[var(--text-muted)]">Last Accessed</div>
                  <div className="text-[var(--text-primary)]">{formatRelativeTime(memory.last_accessed_at)}</div>
                </>
              )}

              <div className="font-medium text-[var(--text-muted)]">ID</div>
              <div className="text-[length:var(--text-sm)] text-[var(--text-primary)]">{memory.id}</div>

              {memory.project_id && (
                <>
                  <div className="font-medium text-[var(--text-muted)]">Project</div>
                  <div className="text-[length:var(--text-sm)] text-[var(--text-primary)]">{memory.project_id}</div>
                </>
              )}
            </div>

            {memory.tags && memory.tags.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[length:var(--text-sm)] font-medium text-[var(--text-muted)]">Tags</div>
                <div className="flex flex-wrap gap-1">
                  {memory.tags.map(tag => (
                    <span key={tag} className={TAG_CLS}>{tag}</span>
                  ))}
                </div>
              </div>
            )}

            <div className="flex gap-2 pt-1.5">
              <button type="button" className={SAVE_BUTTON_CLS} onClick={onEdit}>Edit</button>
              <button
                type="button"
                className={DELETE_BUTTON_CLS}
                onClick={async () => {
                  if (await confirm({ title: 'Delete memory?', description: 'Are you sure you want to delete this memory?', confirmLabel: 'Delete', destructive: true })) {
                    onDelete()
                  }
                }}
              >
                Delete
              </button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
