import { useEffect } from 'react'
import type { GobbyMemory } from '../../hooks/useMemory'
import { formatRelativeTime, typeLabel } from '../../utils/formatTime'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'

interface MemoryDetailProps {
  memory: GobbyMemory | null
  onEdit: () => void
  onDelete: () => void
  onClose: () => void
}

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
        className={`memory-detail-backdrop ${isOpen ? 'open' : ''}`}
        onClick={onClose}
      />
      <div className={`memory-detail-slide ${isOpen ? 'open' : ''}`}>
        {memory && (
          <div className="memory-detail">
            <div className="memory-detail-header">
              <h3>Memory Detail</h3>
              <button className="memory-detail-close" onClick={onClose}>
                &times;
              </button>
            </div>

            <div className="memory-detail-content">{memory.content}</div>

            <div className="memory-detail-grid">
              <div className="memory-detail-label">Type</div>
              <div className="memory-detail-value">{typeLabel(memory.memory_type)}</div>

              <div className="memory-detail-label">Importance</div>
              <div className="memory-detail-value">
                {(memory.importance * 100).toFixed(0)}%
              </div>

              <div className="memory-detail-label">Source</div>
              <div className="memory-detail-value">
                {memory.source_type ?? 'Unknown'}
              </div>

              <div className="memory-detail-label">Created</div>
              <div className="memory-detail-value">
                {formatRelativeTime(memory.created_at)}
              </div>

              <div className="memory-detail-label">Updated</div>
              <div className="memory-detail-value">
                {formatRelativeTime(memory.updated_at)}
              </div>

              <div className="memory-detail-label">Access Count</div>
              <div className="memory-detail-value">{memory.access_count}</div>

              {memory.last_accessed_at && (
                <>
                  <div className="memory-detail-label">Last Accessed</div>
                  <div className="memory-detail-value">
                    {formatRelativeTime(memory.last_accessed_at)}
                  </div>
                </>
              )}

              <div className="memory-detail-label">ID</div>
              <div className="memory-detail-value memory-detail-mono">{memory.id}</div>

              {memory.project_id && (
                <>
                  <div className="memory-detail-label">Project</div>
                  <div className="memory-detail-value memory-detail-mono">
                    {memory.project_id}
                  </div>
                </>
              )}


            </div>

            {memory.tags && memory.tags.length > 0 && (
              <div className="memory-detail-section">
                <div className="memory-detail-section-title">Tags</div>
                <div className="memory-tags">
                  {memory.tags.map(tag => (
                    <span key={tag} className="memory-tag">
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            )}

            <div className="memory-detail-actions">
              <button className="memory-form-btn-save" onClick={onEdit}>
                Edit
              </button>
              <button
                className="memory-delete-btn"
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
