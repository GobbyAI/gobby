import { useCallback, useState } from "react";

import type { Plan } from "../types/plans";

export function usePlans() {
  const [plans, setPlans] = useState<Map<string, Plan>>(new Map());
  const [activePlanId, setActivePlanId] = useState<string | null>(null);

  const createPlan = useCallback(
    (content: string, title = "Implementation Plan"): string => {
      const id = `plan-${crypto.randomUUID()}`;
      const plan: Plan = {
        id,
        title,
        versions: [{ content, timestamp: new Date() }],
        currentVersionIndex: 0,
      };
      setPlans((previous) => {
        const next = new Map(previous);
        next.set(id, plan);
        return next;
      });
      setActivePlanId(id);
      return id;
    },
    [],
  );

  const updatePlan = useCallback(
    (id: string, content: string, messageId?: string) => {
      setPlans((previous) => {
        const existing = previous.get(id);
        if (!existing) return previous;
        const next = new Map(previous);
        const versions = [
          ...existing.versions,
          { content, messageId, timestamp: new Date() },
        ];
        next.set(id, {
          ...existing,
          versions,
          currentVersionIndex: versions.length - 1,
        });
        return next;
      });
    },
    [],
  );

  const openPlan = useCallback((id: string) => {
    setActivePlanId(id);
  }, []);

  const clearPlans = useCallback(() => {
    setPlans(new Map());
    setActivePlanId(null);
  }, []);

  const setPlanVersion = useCallback((id: string, index: number) => {
    setPlans((previous) => {
      const existing = previous.get(id);
      if (!existing || index < 0 || index >= existing.versions.length)
        return previous;
      const next = new Map(previous);
      next.set(id, { ...existing, currentVersionIndex: index });
      return next;
    });
  }, []);

  const activePlan = activePlanId ? (plans.get(activePlanId) ?? null) : null;

  return {
    plans,
    activePlanId,
    activePlan,
    createPlan,
    updatePlan,
    openPlan,
    clearPlans,
    setPlanVersion,
  };
}
