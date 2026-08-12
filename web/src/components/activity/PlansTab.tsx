import { memo, useEffect } from "react";
import type { Plan } from "../../types/plans";
import type { ApprovalOption } from "../../types/chat";
import type { PlanPendingVariant } from "../chat/planPendingSurface";
import { ActivityPanelEmpty, PlansEmptyIcon } from "./ActivityPanelEmpty";
import { PlanReviewCard } from "./PlanReviewCard";

interface PlansTabProps {
  plans: Map<string, Plan>;
  activePlan: Plan | null;
  onOpenPlan: (id: string) => void;
  onSetPlanVersion: (id: string, index: number) => void;
  planPendingApproval?: boolean;
  planApproved?: boolean;
  planApprovalOptions?: ApprovalOption[];
  onApprovePlan?: (option?: ApprovalOption) => void;
  onRequestPlanChanges?: (feedback: string) => void;
  planPendingVariant?: PlanPendingVariant;
}

export const PlansTab = memo(function PlansTab({
  plans,
  activePlan,
  onOpenPlan,
  onSetPlanVersion,
  planPendingApproval,
  planApproved,
  planApprovalOptions,
  onApprovePlan,
  onRequestPlanChanges,
  planPendingVariant,
}: PlansTabProps) {
  const orderedPlans = Array.from(plans.values()).sort((a, b) => {
    const aTime = a.versions[a.versions.length - 1]?.timestamp.getTime() ?? 0;
    const bTime = b.versions[b.versions.length - 1]?.timestamp.getTime() ?? 0;
    return aTime - bTime;
  });
  const latestPlan = orderedPlans[orderedPlans.length - 1] ?? null;

  // Auto-open the latest plan if none is active.
  useEffect(() => {
    if (!activePlan && latestPlan) {
      onOpenPlan(latestPlan.id);
    }
  }, [activePlan, latestPlan, onOpenPlan]);

  if (!latestPlan) {
    return (
      <ActivityPanelEmpty
        icon={<PlansEmptyIcon />}
        heading="Plans"
        body="Plans appear here when the agent proposes one for review"
      />
    );
  }

  // Show the active plan (or latest if none selected).
  const displayPlan = activePlan ?? latestPlan;

  return (
    <PlanReviewCard
      plan={displayPlan}
      planPendingApproval={!!planPendingApproval}
      planApproved={!!planApproved}
      planApprovalOptions={planApprovalOptions}
      onApprovePlan={onApprovePlan}
      onRequestPlanChanges={onRequestPlanChanges}
      onSetVersion={onSetPlanVersion}
      planPendingVariant={planPendingVariant}
    />
  );
});
