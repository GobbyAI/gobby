import { memo, useState, useCallback, useEffect } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { DiffView } from './DiffView'
import type { ChangedFile } from '../../hooks/useFileChanges'

interface FileChangesTabProps {
  changedFiles: ChangedFile[]
  fetchDiff: (path: string) => Promise<string>
}

function statusBadge(status: string) {
  const map: Record<string, { label: string; className: string }> = {
    E: { label: 'E', className: 'file-status-modified' },
    W: { label: 'W', className: 'file-status-added' },
    D: { label: 'D', className: 'file-status-deleted' },
  }
  const info = map[status] || { label: status, className: 'file-status-untracked' }
  return (
    <span className={`file-status-badge ${info.className}`}>
      {info.label}
    </span>
  )
}

function fileName(path: string): string {
  return path.split('/').pop() || path
}

function fileDir(path: string): string {
  const parts = path.split('/')
  if (parts.length <= 1) return ''
  return parts.slice(0, -1).join('/') + '/'
}

export const FileChangesTab = memo(function FileChangesTab({
  changedFiles,
  fetchDiff,
}: FileChangesTabProps) {
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [diff, setDiff] = useState<string>('')
  const [loadingDiff, setLoadingDiff] = useState(false)
  const [topHeight, setTopHeight] = useState(35)

  const handleSelect = useCallback(
    (path: string) => {
      if (path === selectedPath) {
        setSelectedPath(null)
        setDiff('')
        return
      }
      setSelectedPath(path)
    },
    [selectedPath]
  )

  useEffect(() => {
    if (!selectedPath) {
      setDiff('')
      setLoadingDiff(false)
      return
    }
    if (!changedFiles.some((file) => file.path === selectedPath)) {
      setSelectedPath(null)
      setDiff('')
      setLoadingDiff(false)
      return
    }
    let isStale = false
    setLoadingDiff(true)
    void fetchDiff(selectedPath)
      .then((result) => {
        if (!isStale) {
          setDiff(result)
        }
      })
      .catch((err) => {
        if (!isStale) {
          console.error('Failed to fetch diff:', err)
          setDiff('')
        }
      })
      .finally(() => {
        if (!isStale) {
          setLoadingDiff(false)
        }
      })
    return () => {
      isStale = true
    }
  }, [changedFiles, fetchDiff, selectedPath])

  if (changedFiles.length === 0) {
    return (
      <div className="activity-tab-empty">
        <p>No file changes detected</p>
        <p className="text-xs text-muted-foreground mt-1">
          Changes will appear here as files are modified during the session
        </p>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* File list */}
      <div
        className={`overflow-y-auto ${selectedPath ? 'border-b border-border' : 'flex-1'}`}
        style={selectedPath ? { height: `${topHeight}%` } : undefined}
      >
        <div className="flex items-center justify-between px-3 py-2 border-b border-border bg-muted/20">
          <span className="text-xs text-muted-foreground">
            {changedFiles.length} file{changedFiles.length !== 1 ? 's' : ''} changed
          </span>
        </div>
        {changedFiles.map((file) => {
          const isSelected = file.path === selectedPath
          return (
            <button
              key={file.path}
              onClick={() => handleSelect(file.path)}
              aria-pressed={isSelected}
              className={`w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-muted transition-colors border-b border-border/50 ${isSelected ? 'bg-muted/50' : ''}`}
            >
              {statusBadge(file.status)}
              <div className="flex-1 min-w-0 flex items-baseline gap-1">
                <span className="text-sm font-medium truncate">
                  {fileName(file.path)}
                </span>
                <span className="text-[10px] text-muted-foreground truncate">
                  {fileDir(file.path)}
                </span>
              </div>
            </button>
          )
        })}
      </div>

      {/* Resize handle */}
      {selectedPath && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {/* Diff area */}
      {selectedPath && (
        <div className="flex-1 flex flex-col min-h-0">
          {loadingDiff ? (
            <div className="activity-tab-empty">
              <p>Loading diff...</p>
            </div>
          ) : (
            <DiffView diff={diff} path={selectedPath} />
          )}
        </div>
      )}
    </div>
  )
})
