import { createContext, useContext, type ReactNode } from 'react'

interface PlanCapability {
  /**
   * True when the active CLI cannot switch out of plan mode automatically on
   * approval (no protocol-level mode push). The approval UI notes that a manual
   * continue is required. Defaults false (native CLIs auto-switch).
   */
  manualSwitchRequired: boolean
}

const PlanCapabilityContext = createContext<PlanCapability>({
  manualSwitchRequired: false,
})

export function PlanCapabilityProvider({
  manualSwitchRequired,
  children,
}: {
  manualSwitchRequired: boolean
  children: ReactNode
}) {
  return (
    <PlanCapabilityContext.Provider value={{ manualSwitchRequired }}>
      {children}
    </PlanCapabilityContext.Provider>
  )
}

// Co-locating the provider and its hook in one context module is intentional;
// react-refresh's component-only-export constraint does not apply to a tiny
// context file with no stateful component logic to hot-reload.
// eslint-disable-next-line react-refresh/only-export-components
export function usePlanCapability(): PlanCapability {
  return useContext(PlanCapabilityContext)
}
