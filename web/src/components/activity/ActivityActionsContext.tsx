import { useCallback, useMemo, useState, type ReactNode } from "react";

import {
  ActivityActionsContext,
  useActivityActions,
  type ActivityPanelActions,
} from "./activityActions";

export function ActivityActionsProvider({ children }: { children: ReactNode }) {
  const [actions, setActions] = useState<ActivityPanelActions | null>(null);
  const register = useCallback((next: ActivityPanelActions | null) => {
    setActions(next);
  }, []);
  const value = useMemo(() => ({ actions, register }), [actions, register]);
  return (
    <ActivityActionsContext.Provider value={value}>
      {children}
    </ActivityActionsContext.Provider>
  );
}

function RefreshGlyph() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
      <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
    </svg>
  );
}

function PlusGlyph() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

/**
 * Renders the active tab's registered Add/Refresh buttons. Placed in the panel
 * header between the dropdown and the Hide Chat slot. Returns nothing when the
 * active tab registers no actions.
 */
export function ActivityActionButtons() {
  const actions = useActivityActions();
  if (!actions || (!actions.onAdd && !actions.onRefresh)) return null;

  const refreshLabel = actions.refreshing
    ? "Refreshing"
    : (actions.refreshLabel ?? "Refresh");
  const addLabel = actions.addLabel ?? "Add";

  return (
    <span className="activity-panel-actions-slot">
      {actions.onRefresh && (
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn"
          onClick={actions.onRefresh}
          disabled={actions.refreshing}
          aria-label={actions.refreshAriaLabel ?? refreshLabel}
          title={actions.refreshAriaLabel ?? refreshLabel}
        >
          <RefreshGlyph />
          <span className="activity-panel-action-btn__label">{refreshLabel}</span>
        </button>
      )}
      {actions.onAdd && (
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn"
          onClick={actions.onAdd}
          disabled={actions.addDisabled}
          aria-label={actions.addAriaLabel ?? addLabel}
          title={actions.addAriaLabel ?? addLabel}
        >
          <PlusGlyph />
          <span className="activity-panel-action-btn__label">{addLabel}</span>
        </button>
      )}
    </span>
  );
}
