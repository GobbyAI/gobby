import { memo, useEffect } from 'react'
import type { Artifact } from '../../types/artifacts'
import type { ApprovalOption } from '../../types/chat'
import type { PlanPendingVariant } from '../chat/planPendingSurface'
import { ActivityPanelEmpty, PlansEmptyIcon } from './ActivityPanelEmpty'
import { PlanReviewCard } from './PlanReviewCard'

interface PlansTabProps {
  artifacts: Map<string, Artifact>
  artifact: Artifact | null
  onOpenArtifact: (id: string) => void
  // Accepted for caller compatibility; the review surface manages its own chrome.
  onClose?: () => void
  onUpdateContent?: (id: string, content: string) => void
  onSetVersion: (id: string, index: number) => void
  planPendingApproval?: boolean
  planApproved?: boolean
  planApprovalOptions?: ApprovalOption[]
  onApprovePlan?: (option?: ApprovalOption) => void
  onRequestPlanChanges?: (feedback: string) => void
  planPendingVariant?: PlanPendingVariant
}

export const PlansTab = memo(function PlansTab({
  artifacts,
  artifact,
  onOpenArtifact,
  onSetVersion,
  planPendingApproval,
  planApproved,
  planApprovalOptions,
  onApprovePlan,
  onRequestPlanChanges,
  planPendingVariant,
}: PlansTabProps) {
  // Only plan artifacts, oldest -> newest by latest version timestamp.
  const plans = Array.from(artifacts.values())
    .filter((a) => a.isPlan)
    .sort((a, b) => {
      const aTime = a.versions[a.versions.length - 1]?.timestamp.getTime() ?? 0
      const bTime = b.versions[b.versions.length - 1]?.timestamp.getTime() ?? 0
      return aTime - bTime
    })
  const latestPlan = plans[plans.length - 1] ?? null

  // Auto-open the latest plan if none is active.
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

  // Show the active plan (or latest if none selected).
  const displayPlan = artifact?.isPlan ? artifact : latestPlan

  return (
    <PlanReviewCard
      plan={displayPlan}
      planPendingApproval={!!planPendingApproval}
      planApproved={!!planApproved}
      planApprovalOptions={planApprovalOptions}
      onApprovePlan={onApprovePlan}
      onRequestPlanChanges={onRequestPlanChanges}
      onSetVersion={onSetVersion}
      planPendingVariant={planPendingVariant}
    />
  )
})
