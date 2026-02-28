import { useState, useCallback, memo } from 'react'
import type { Artifact } from '../../../types/artifacts'
import { Button } from '../ui/Button'
import { Badge } from '../ui/Badge'
import { ArtifactCodeView } from './ArtifactCodeView'
import { ArtifactTextView } from './ArtifactTextView'
import { ArtifactImageView } from './ArtifactImageView'
import { ArtifactSheetView } from './ArtifactSheetView'
import { ArtifactVersionBar } from './ArtifactVersionBar'
import { PlanApprovalBar } from '../PlanApprovalBar'

interface ArtifactPanelProps {
  artifact: Artifact
  width?: number  // undefined = full width (mobile)
  onClose: () => void
  onUpdateContent?: (id: string, content: string) => void
  onSetVersion: (id: string, index: number) => void
  planPendingApproval?: boolean
  onApprovePlan?: () => void
  onRequestPlanChanges?: (feedback: string) => void
}

export const ArtifactPanel = memo(function ArtifactPanel({ artifact, width, onClose, onUpdateContent, onSetVersion, planPendingApproval, onApprovePlan, onRequestPlanChanges }: ArtifactPanelProps) {
  const [isEditing, setIsEditing] = useState(false)
  const [showSource, setShowSource] = useState(false)

  const versionIndex = Math.max(0, Math.min(artifact.currentVersionIndex, artifact.versions.length - 1))
  const currentVersion = artifact.versions[versionIndex]
  const content = currentVersion?.content ?? ''

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content)
    } catch {
      console.error('Failed to copy to clipboard')
    }
  }, [content])

  const handleDownload = useCallback(() => {
    const ext = artifact.type === 'code' ? (artifact.language || 'txt') : artifact.type === 'image' ? 'png' : artifact.type === 'sheet' ? 'csv' : 'txt'
    let blob: Blob
    if (artifact.type === 'image' && content.startsWith('data:')) {
      const commaIdx = content.indexOf(',')
      if (commaIdx === -1) {
        blob = new Blob([content], { type: 'application/octet-stream' })
      } else {
        const header = content.slice(0, commaIdx)
        const b64 = content.slice(commaIdx + 1)
        const mime = header.match(/data:(.*?);/)?.[1] || 'application/octet-stream'
        try {
          const binary = atob(b64)
          const bytes = new Uint8Array(binary.length)
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
          blob = new Blob([bytes], { type: mime })
        } catch {
          blob = new Blob([content], { type: 'application/octet-stream' })
        }
      }
    } else {
      const mimeType = artifact.type === 'image' ? 'application/octet-stream' : 'text/plain'
      blob = new Blob([content], { type: mimeType })
    }
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const safeName = artifact.title.replace(/[^\w\s.-]/g, '').replace(/\s+/g, '_') || 'artifact'
    a.download = `${safeName}.${ext}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 100)
  }, [content, artifact])

  // Determine which header toggle to show
  const isEditable = (artifact.type === 'code' || artifact.type === 'text') && !!onUpdateContent
  const isTextReadOnly = artifact.type === 'text' && !onUpdateContent

  return (
    <div
      className="flex flex-col h-full border-l border-border bg-background"
      style={width ? { width, flexShrink: 0 } : { width: '100%' }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-foreground truncate">{artifact.title}</div>
          <div className="flex items-center gap-1.5 mt-0.5">
            <Badge variant="default">{artifact.type}</Badge>
            {artifact.language && (
              <span className="text-xs text-muted-foreground font-mono">{artifact.language}</span>
            )}
          </div>
        </div>
        <Button size="icon" variant="ghost" onClick={handleCopy} title="Copy">
          <CopyIcon />
        </Button>
        <Button size="icon" variant="ghost" onClick={handleDownload} title="Download">
          <DownloadIcon />
        </Button>
        {isEditable && (
          <Button size="icon" variant="ghost" onClick={() => setIsEditing(!isEditing)} title={isEditing ? 'View' : 'Edit'}>
            {isEditing ? <ViewIcon /> : <EditIcon />}
          </Button>
        )}
        {isTextReadOnly && (
          <Button size="icon" variant="ghost" onClick={() => setShowSource(!showSource)} title={showSource ? 'Preview' : 'Source'}>
            {showSource ? <ViewIcon /> : <SourceIcon />}
          </Button>
        )}
        <Button size="icon" variant="ghost" onClick={onClose} title="Close panel">
          <CloseIcon />
        </Button>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 overflow-hidden">
        {artifact.type === 'code' && (
          <ArtifactCodeView
            content={content}
            language={artifact.language}
            isEditing={isEditing}
            onChange={onUpdateContent ? (c) => onUpdateContent(artifact.id, c) : undefined}
          />
        )}
        {artifact.type === 'text' && (
          <ArtifactTextView
            content={content}
            artifactId={artifact.id}
            isEditing={isEditing}
            showSource={showSource}
            onChange={onUpdateContent ? (c) => onUpdateContent(artifact.id, c) : undefined}
          />
        )}
        {artifact.type === 'image' && (
          <ArtifactImageView content={content} />
        )}
        {artifact.type === 'sheet' && (
          <ArtifactSheetView content={content} />
        )}
      </div>

      {/* Plan approval */}
      {planPendingApproval && onApprovePlan && onRequestPlanChanges && (
        <PlanApprovalBar onApprove={onApprovePlan} onRequestChanges={onRequestPlanChanges} />
      )}

      {/* Version bar */}
      <ArtifactVersionBar
        artifact={artifact}
        onSetVersion={(index) => onSetVersion(artifact.id, index)}
      />
    </div>
  )
})

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function EditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </svg>
  )
}

function ViewIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  )
}

function SourceIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  )
}
