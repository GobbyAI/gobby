import { useCallback, useMemo, useState, type ReactNode } from "react";

import { Button } from "../ui/Button";
import { SegmentedControl } from "../ui/SegmentedControl";
import {
  ActivityActionsContext,
  useActivityActions,
  type ActivityPanelActions,
} from "./activityActions";
import { FilterDropdownTrigger } from "./FilterPrimitives";

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

function FilterGlyph() {
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
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
    </svg>
  );
}

function SearchGlyph() {
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
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

/**
 * Renders the active tab's registered toolbar: selector, then Filter / Search /
 * Refresh / New triggers. Placed in the panel header between the dropdown and
 * the Hide Chat slot. Returns nothing when the active tab registers no actions.
 */
export function ActivityActionButtons() {
  const actions = useActivityActions();
  if (
    !actions ||
    (!actions.selector &&
      !actions.filter &&
      !actions.search &&
      !actions.onAdd &&
      !actions.onRefresh)
  ) {
    return null;
  }

  const refreshLabel = actions.refreshing
    ? "Refreshing"
    : (actions.refreshLabel ?? "Refresh");
  const addLabel = actions.addLabel ?? "New";

  return (
    <span className="activity-panel-actions-slot flex min-w-0 shrink items-center gap-2">
      {actions.selector && (
        <SegmentedControl<string>
          value={actions.selector.value}
          onChange={actions.selector.onChange}
          options={actions.selector.options}
          ariaLabel={actions.selector.ariaLabel}
          controlHeight="sm"
          className="activity-panel-header-segmented min-w-0 shrink @max-[479px]/activity-panel:[&>.segmented-control__option]:px-2"
        />
      )}
      {actions.filter && (
        <FilterDropdownTrigger
          open={actions.filter.open}
          activeCount={actions.filter.activeCount}
          icon={<FilterGlyph />}
          onClick={actions.filter.onToggle}
          aria-label={actions.filter.ariaLabel}
          title={actions.filter.ariaLabel}
        />
      )}
      {actions.search && (
        <Button
          type="button"
          variant="accent"
          size="sm"
          onClick={actions.search.onToggle}
          aria-label={actions.search.ariaLabel}
          title={actions.search.ariaLabel}
          aria-expanded={actions.search.open}
        >
          <SearchGlyph />
          <span className="activity-panel-action-btn__label @max-[479px]/activity-panel:hidden">
            Search
          </span>
        </Button>
      )}
      {actions.onRefresh && (
        <Button
          type="button"
          variant="accent"
          size="sm"
          onClick={actions.onRefresh}
          disabled={actions.refreshing}
          aria-label={actions.refreshAriaLabel ?? refreshLabel}
          title={actions.refreshAriaLabel ?? refreshLabel}
        >
          <RefreshGlyph />
          <span className="activity-panel-action-btn__label @max-[479px]/activity-panel:hidden">
            {refreshLabel}
          </span>
        </Button>
      )}
      {actions.onAdd && (
        <Button
          type="button"
          variant="accent"
          size="sm"
          onClick={actions.onAdd}
          disabled={actions.addDisabled}
          aria-label={actions.addAriaLabel ?? addLabel}
          title={actions.addAriaLabel ?? addLabel}
        >
          <PlusGlyph />
          <span className="activity-panel-action-btn__label @max-[479px]/activity-panel:hidden">
            {addLabel}
          </span>
        </Button>
      )}
    </span>
  );
}
