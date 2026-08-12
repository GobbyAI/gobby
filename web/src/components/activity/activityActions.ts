import {
  createContext,
  useContext,
  useEffect,
  type DependencyList,
} from "react";

/**
 * Shared, context-aware toolbar for the activity panel header.
 *
 * The header (between the view dropdown and the Hide Chat button) renders one
 * toolbar whose contents follow the active tab: an optional segmented view
 * selector, then Filter / Search / New triggers. Each tab registers
 * what it supports via {@link useRegisterActivityActions}; tabs that register
 * nothing render no controls. The actual surfaces (filter dropdowns, search
 * bars, modals) stay inside the tab — the header controls are dumb triggers,
 * except the selector, which carries its options and value here so the header
 * can render one canonical SegmentedControl for every tab.
 */
export interface ActivityToolbarSelector<Option extends string = string> {
  value: Option;
  onChange: (value: Option) => void;
  options: readonly { value: Option; label: string }[];
  ariaLabel: string;
}

/** Trigger for the tab-rendered filter dropdown. */
export interface ActivityToolbarFilter {
  open: boolean;
  onToggle: () => void;
  ariaLabel: string;
  /** Applied-filter count rendered as a badge when > 0. */
  activeCount?: number;
}

/** Toggle for the tab's hidden-by-default search bar. */
export interface ActivityToolbarSearch {
  open: boolean;
  onToggle: () => void;
  ariaLabel: string;
}

export interface ActivityPanelActions<Option extends string = string> {
  selector?: ActivityToolbarSelector<Option>;
  filter?: ActivityToolbarFilter;
  search?: ActivityToolbarSearch;
  onAdd?: () => void;
  addLabel?: string;
  addAriaLabel?: string;
  addDisabled?: boolean;
}

export interface ActivityActionsContextValue {
  actions: ActivityPanelActions | null;
  register: (actions: ActivityPanelActions | null) => void;
}

export const ActivityActionsContext =
  createContext<ActivityActionsContextValue>({
    actions: null,
    register: () => {},
  });

export function useActivityActions(): ActivityPanelActions | null {
  return useContext(ActivityActionsContext).actions;
}

/**
 * Register the active tab's header actions. Pass a `deps` list of the values
 * the action object closes over (callbacks + live flags like `open`);
 * the registration refreshes only when they change, and clears on unmount.
 * Safe to call with no provider present (e.g. in isolated tests) — it is a
 * no-op then.
 */
export function useRegisterActivityActions<Option extends string = string>(
  actions: ActivityPanelActions<Option> | null,
  deps: DependencyList,
): void {
  const { register } = useContext(ActivityActionsContext);
  useEffect(() => {
    register(actions as ActivityPanelActions | null);
    return () => register(null);
    // actions is intentionally tracked via the caller-supplied deps so the
    // hook does not require a memoized object at every call site.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [register, ...deps]);
}
