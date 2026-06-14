import { memo, useState, useCallback, useEffect, useMemo } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { DiffBlock } from '../shared/DiffBlock'
import { parseUnifiedDiffLines } from '../shared/DiffBlock.helpers'
import type { ChangedFile } from '../../hooks/useFileChanges'
import { ActivityPanelEmpty, ChangesEmptyIcon } from './ActivityPanelEmpty'

interface FileChangesTabProps {
  changedFiles: ChangedFile[]
  fetchDiff: (path: string) => Promise<string>
  loading?: boolean
  error?: string | null
  onRetry?: () => void
}

// Cap rendered diffs so a single huge file can't lock up the panel.
const MAX_DIFF_LINES = 2000

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
  loading = false,
  error = null,
  onRetry,
}: FileChangesTabProps) {
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [diff, setDiff] = useState<string>('')
  const [loadingDiff, setLoadingDiff] = useState(false)
  const [topHeight, setTopHeight] = useState(35)

  const { displayedDiff, diffTruncated } = useMemo(() => {
    const lines = diff.split('\n')
    if (lines.length <= MAX_DIFF_LINES) return { displayedDiff: diff, diffTruncated: false }
    return { displayedDiff: lines.slice(0, MAX_DIFF_LINES).join('\n'), diffTruncated: true }
  }, [diff])

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

  if (error && changedFiles.length === 0) {
    return (
      <ActivityPanelEmpty
        icon={<ChangesEmptyIcon />}
        heading="Changes unavailable"
        body={error}
        footer={
          onRetry ? (
            <button
              type="button"
              onClick={onRetry}
              className="mt-3 rounded border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted transition-colors"
            >
              Retry
            </button>
          ) : undefined
        }
      />
    )
  }

  if (loading && changedFiles.length === 0) {
    return <ActivityPanelEmpty icon={<ChangesEmptyIcon />} body="Loading changes…" />
  }

  if (changedFiles.length === 0) {
    return (
      <ActivityPanelEmpty
        icon={<ChangesEmptyIcon />}
        heading="Changes"
        body="Changes appear here as files are modified during the session"
      />
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
                <span className="text-2xs text-muted-foreground truncate">
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
            <ActivityPanelEmpty body="Loading diff…" />
          ) : !diff ? (
            <ActivityPanelEmpty body="No diff available for this file" />
          ) : (
            <>
              {diffTruncated && (
                <div className="px-3 py-1.5 text-xs text-muted-foreground border-b border-border bg-muted/20">
                  Large diff truncated to the first {MAX_DIFF_LINES.toLocaleString()} lines.
                </div>
              )}
              <DiffBlock
                lines={parseUnifiedDiffLines(displayedDiff)}
                language="diff"
                variant="inline"
                path={selectedPath}
                header
                onCopy={() => {
                  navigator.clipboard.writeText(diff).catch(console.error)
                }}
              />
            </>
          )}
        </div>
      )}
    </div>
  )
})
