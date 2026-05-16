import { memo } from 'react'
import type { Artifact } from '../../types/artifacts'
import { ArtifactPanel } from '../chat/artifacts/ArtifactPanel'
import { ActivityPanelEmpty, ArtifactsEmptyIcon } from './ActivityPanelEmpty'
import { Heading } from '../shared/Heading'

interface ArtifactsTabProps {
  artifacts: Map<string, Artifact>
  artifact: Artifact | null
  onOpenArtifact: (id: string) => void
  onClose: () => void
  onMinimize?: () => void
  onMaximize?: () => void
  isMaximized?: boolean
  onUpdateContent?: (id: string, content: string) => void
  onSetVersion: (id: string, index: number) => void
  planPendingApproval?: boolean
  onApprovePlan?: () => void
  onRequestPlanChanges?: (feedback: string) => void
  onClearAll?: () => void
}

export const ArtifactsTab = memo(function ArtifactsTab({
  artifacts,
  artifact,
  onOpenArtifact,
  onClose,
  onMinimize,
  onMaximize,
  isMaximized,
  onUpdateContent,
  onSetVersion,
  planPendingApproval,
  onApprovePlan,
  onRequestPlanChanges,
  onClearAll,
}: ArtifactsTabProps) {
  const artifactList = Array.from(artifacts.values()).reverse()

  const renderHistory = (isMini = false) => {
    if (artifactList.length === 0) {
      return (
        <ActivityPanelEmpty
          icon={<ArtifactsEmptyIcon />}
          heading="Artifacts"
          body="Artifacts appear here when code, text, or plans are generated"
        />
      )
    }

    return (
      <div className={`flex flex-col h-full bg-background ${isMini ? 'border-r border-border w-64' : ''}`}>
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-muted/20">
          <Heading level={2} className="text-sm font-semibold truncate">{isMini ? 'History' : 'Artifact History'}</Heading>
          <div className="flex items-center gap-2 shrink-0">
            {!isMini && <span className="text-xs text-muted-foreground">{artifactList.length} items</span>}
            {onClearAll && artifactList.length > 0 && (
              <button
                onClick={onClearAll}
                className="p-1 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors"
                title="Clear all artifacts"
              >
                <TrashIcon />
              </button>
            )}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {artifactList.map((a) => (
            <button
              key={a.id}
              onClick={() => onOpenArtifact(a.id)}
              className={`w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-muted transition-colors border-b border-border/50 group ${artifact?.id === a.id ? 'bg-muted/50' : ''}`}
            >
              <div className="flex-1 min-w-0">
                <div className={`text-sm font-medium truncate transition-colors ${artifact?.id === a.id ? 'text-primary' : 'group-hover:text-primary'}`}>
                  {a.title}
                </div>
                <div className="flex items-center gap-2 mt-1 overflow-hidden">
                  <span className="text-[10px] font-mono text-muted-foreground uppercase px-1.5 py-0.5 rounded bg-muted-foreground/10 shrink-0">
                    {a.type}
                  </span>
                  {!isMini && a.language && (
                    <span className="text-[10px] text-muted-foreground font-mono truncate">{a.language}</span>
                  )}
                  {!isMini && (
                    <span className="text-[10px] text-muted-foreground ml-auto shrink-0">
                      v{a.versions.length}
                    </span>
                  )}
                </div>
              </div>
              {!isMini && <ChevronRightIcon />}
            </button>
          ))}
        </div>
      </div>
    )
  }

  if (!artifact) {
    return renderHistory(false)
  }

  // Side-by-side view when maximized
  if (isMaximized) {
    return (
      <div className="flex h-full overflow-hidden bg-background">
        {renderHistory(true)}
        <div className="flex-1 min-w-0 overflow-hidden">
          <ArtifactPanel
            artifact={artifact}
            onClose={onClose}
            onMinimize={onMinimize}
            onMaximize={onMaximize}
            isMaximized={isMaximized}
            onBack={() => onClose()}
            onUpdateContent={onUpdateContent}
            onSetVersion={onSetVersion}
            planPendingApproval={planPendingApproval}
            onApprovePlan={onApprovePlan}
            onRequestPlanChanges={onRequestPlanChanges}
          />
        </div>
      </div>
    )
  }

  // Mobile/Standard single view
  return (
    <ArtifactPanel
      artifact={artifact}
      onClose={onClose}
      onMinimize={onMinimize}
      onMaximize={onMaximize}
      isMaximized={isMaximized}
      onBack={() => onClose()}
      onUpdateContent={onUpdateContent}
      onSetVersion={onSetVersion}
      planPendingApproval={planPendingApproval}
      onApprovePlan={onApprovePlan}
      onRequestPlanChanges={onRequestPlanChanges}
    />
  )
})

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  )
}

function ChevronRightIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-muted-foreground">
      <polyline points="9 18 15 12 9 6" />
    </svg>
  )
}
