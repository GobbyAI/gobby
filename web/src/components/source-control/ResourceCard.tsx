import { useState } from 'react'
import { StatusBadge } from './StatusBadge'
import type { ResourceField } from './resourceCardUtils'

export interface ResourceCardProps {
  id: string
  title: string
  status: string
  fields: ResourceField[]
  onSync?: (id: string) => Promise<unknown>
  onDelete?: (id: string) => Promise<unknown>
}

export function ResourceCard({ id, title, status, fields, onSync, onDelete }: ResourceCardProps) {
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [loading, setLoading] = useState<'sync' | 'delete' | null>(null)

  const handleSync = async () => {
    if (!onSync) return
    setLoading('sync')
    try {
      await onSync(id)
    } finally {
      setLoading(null)
    }
  }

  const handleDelete = async () => {
    if (!onDelete) return
    setLoading('delete')
    try {
      await onDelete(id)
    } finally {
      setLoading(null)
      setConfirmDelete(false)
    }
  }

  return (
    <div className="sc-card">
      <div className="sc-card__header">
        <span className="sc-card__title">{title}</span>
        <StatusBadge status={status} />
      </div>
      <div className="sc-card__body">
        {fields.map((f, i) => (
          <div key={`${f.label}-${i}`} className="sc-card__field">
            <span className="sc-card__label">{f.label}</span>
            {f.code ? (
              <code className="sc-card__value">{f.value}</code>
            ) : (
              <span className={`sc-card__value${f.muted ? ' sc-text-muted' : ''}`}>{f.value}</span>
            )}
          </div>
        ))}
      </div>
      {(onSync || onDelete) && (
        <div className="sc-card__actions">
          {onSync && (
            <button
              className="sc-btn sc-btn--sm"
              onClick={handleSync}
              disabled={loading !== null}
            >
              {loading === 'sync' ? 'Syncing...' : 'Sync'}
            </button>
          )}
          {onDelete && (
            confirmDelete ? (
              <>
                <button
                  className="sc-btn sc-btn--sm sc-btn--danger"
                  onClick={handleDelete}
                  disabled={loading !== null}
                >
                  {loading === 'delete' ? 'Deleting...' : 'Confirm'}
                </button>
                <button
                  className="sc-btn sc-btn--sm"
                  onClick={() => setConfirmDelete(false)}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                className="sc-btn sc-btn--sm sc-btn--danger"
                onClick={() => setConfirmDelete(true)}
              >
                Delete
              </button>
            )
          )}
        </div>
      )}
    </div>
  )
}
