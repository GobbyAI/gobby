import {
  createContext,
  useContext,
  useEffect,
  type DependencyList,
} from "react";

/**
 * Shared, context-aware Add/Refresh actions for the activity panel header.
 *
 * The header (between the view dropdown and the Hide Chat button) renders a
 * single pair of Add/Refresh buttons whose behaviour follows the active tab.
 * Each tab registers what it supports via {@link useRegisterActivityActions};
 * tabs that register neither action render no buttons. The actual surfaces
 * (modals, forms) stay inside the tab — the header buttons are dumb triggers.
 */
export interface ActivityPanelActions {
  onAdd?: () => void;
  addLabel?: string;
  addAriaLabel?: string;
  addDisabled?: boolean;
  onRefresh?: () => void;
  refreshLabel?: string;
  refreshAriaLabel?: string;
  refreshing?: boolean;
}

export interface ActivityActionsContextValue {
  actions: ActivityPanelActions | null;
  register: (actions: ActivityPanelActions | null) => void;
}

export const ActivityActionsContext = createContext<ActivityActionsContextValue>({
  actions: null,
  register: () => {},
});

export function useActivityActions(): ActivityPanelActions | null {
  return useContext(ActivityActionsContext).actions;
}

/**
 * Register the active tab's header actions. Pass a `deps` list of the values
 * the action object closes over (callbacks + live flags like `refreshing`);
 * the registration refreshes only when they change, and clears on unmount.
 * Safe to call with no provider present (e.g. in isolated tests) — it is a
 * no-op then.
 */
export function useRegisterActivityActions(
  actions: ActivityPanelActions | null,
  deps: DependencyList,
): void {
  const { register } = useContext(ActivityActionsContext);
  useEffect(() => {
    register(actions);
    return () => register(null);
    // actions is intentionally tracked via the caller-supplied deps so the
    // hook does not require a memoized object at every call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [register, ...deps]);
}
