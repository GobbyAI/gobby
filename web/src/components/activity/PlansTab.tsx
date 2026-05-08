import { memo, useEffect } from 'react'
import type { Artifact } from '../../types/artifacts'
import { ArtifactPanel } from '../chat/artifacts/ArtifactPanel'
import { ActivityPanelEmpty, PlansEmptyIcon } from './ActivityPanelEmpty'

interface PlansTabProps {
  artifacts: Map<string, Artifact>
  artifact: Artifact | null
  onOpenArtifact: (id: string) => void
  onClose: () => void
  onUpdateContent?: (id: string, content: string) => void
  onSetVersion: (id: string, index: number) => void
  planPendingApproval?: boolean
  onApprovePlan?: () => void
  onRequestPlanChanges?: (feedback: string) => void
}

export const PlansTab = memo(function PlansTab({
  artifacts,
  artifact,
  onOpenArtifact,
  onClose,
  onUpdateContent,
  onSetVersion,
  planPendingApproval,
  onApprovePlan,
  onRequestPlanChanges,
}: PlansTabProps) {
  // Only plan artifacts, sorted by most recent version timestamp
  const plans = Array.from(artifacts.values())
    .filter((a) => a.isPlan)
    .sort((a, b) => {
      const aTime = a.versions[a.versions.length - 1]?.timestamp.getTime() ?? 0
      const bTime = b.versions[b.versions.length - 1]?.timestamp.getTime() ?? 0
      return aTime - bTime
    })
  const latestPlan = plans[plans.length - 1] ?? null

  // Auto-open the latest plan if none is active
  useEffect(() => {
    if (!artifact && latestPlan) {
      onOpenArtifact(latestPlan.id)
    }
  }, [artifact, latestPlan, onOpenArtifact])

  if (!latestPlan) {
    return (
      <ActivityPanelEmpty
        icon={<PlansEmptyIcon />}
        heading="Plans"
        body="Plans appear here when the agent proposes one for review"
      />
    )
  }

  // Show the active plan (or latest if none selected)
  const displayPlan = artifact?.isPlan ? artifact : latestPlan

  return (
    <ArtifactPanel
      artifact={displayPlan}
      onClose={onClose}
      onBack={onClose}
      onUpdateContent={onUpdateContent}
      onSetVersion={onSetVersion}
      planPendingApproval={planPendingApproval}
      onApprovePlan={onApprovePlan}
      onRequestPlanChanges={onRequestPlanChanges}
    />
  )
})
