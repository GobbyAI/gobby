import { useMemo } from "react";

import { SegmentedControl } from "../ui/SegmentedControl";
import type { StageRegistryEntry } from "../../hooks/useStagesRegistry";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { TasksTabFilters } from "./TasksTabFilters";
import type { TaskFilterKey } from "./TasksTabModel";

export type TasksViewMode = "list" | "board";

/** Stacked rows — shape carries the meaning, not color (deutan-safe). */
function ListGlyph() {
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
      <line x1="8" y1="6" x2="21" y2="6" />
      <line x1="8" y1="12" x2="21" y2="12" />
      <line x1="8" y1="18" x2="21" y2="18" />
      <line x1="3" y1="6" x2="3.01" y2="6" />
      <line x1="3" y1="12" x2="3.01" y2="12" />
      <line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  );
}

/** Three columns — distinct silhouette from the list rows in monochrome. */
function BoardGlyph() {
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
      <rect x="3" y="4" width="5" height="16" rx="1" />
      <rect x="9.5" y="4" width="5" height="11" rx="1" />
      <rect x="16" y="4" width="5" height="14" rx="1" />
    </svg>
  );
}

interface TasksTabToolbarProps {
  search: string;
  onSearchChange: (value: string) => void;
  viewMode: TasksViewMode;
  onViewModeChange: (mode: TasksViewMode) => void;
  showFilterDropdown: boolean;
  onToggleFilterDropdown: () => void;
  activeFilterCount: number;
  statusFilters: Set<TaskFilterKey>;
  stagesRegistry: StageRegistryEntry[];
  selectedStageFilters: ReadonlySet<string>;
  onFiltersApply: (filters: Set<TaskFilterKey>, stages: Set<string>) => void;
  onCloseFilterDropdown: () => void;
}

/**
 * D6 — Tasks toolbar: search, the List/Board view switcher, and the filter
 * affordance. The switcher is a shared `SegmentedControl` (keyboard-operable,
 * theme-aware, AA focus ring); icons are shape-distinct so the choice reads
 * in a grayscale screenshot.
 */
export function TasksTabToolbar({
  search,
  onSearchChange,
  viewMode,
  onViewModeChange,
  showFilterDropdown,
  onToggleFilterDropdown,
  activeFilterCount,
  statusFilters,
  stagesRegistry,
  selectedStageFilters,
  onFiltersApply,
  onCloseFilterDropdown,
}: TasksTabToolbarProps) {
  const viewOptions = useMemo(
    () =>
      [
        {
          value: "list" as const,
          label: <ListGlyph />,
          ariaLabel: "List view",
          title: "List view",
        },
        {
          value: "board" as const,
          label: <BoardGlyph />,
          ariaLabel: "Board view",
          title: "Board view (lifecycle stages)",
        },
      ] as const,
    [],
  );

  return (
    <div className="activity-panel-toolbar">
      <ActivityPanelSearch
        value={search}
        onChange={onSearchChange}
        placeholder="Search"
      />
      <SegmentedControl
        className="activity-panel-toolbar-segmented"
        ariaLabel="Task view"
        value={viewMode}
        onChange={onViewModeChange}
        options={viewOptions}
        controlHeight="sm"
      />
      <button
        type="button"
        className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
        onClick={onToggleFilterDropdown}
        title="Filter by task state"
        aria-label="Filter tasks"
        aria-expanded={showFilterDropdown}
      >
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
        <span className="activity-panel-action-btn__label">Filter</span>
        {activeFilterCount > 0 && (
          <span className="activity-filter-badge">{activeFilterCount}</span>
        )}
      </button>
      {showFilterDropdown && (
        <TasksTabFilters
          filters={statusFilters}
          stages={stagesRegistry}
          selectedStages={selectedStageFilters}
          onApply={onFiltersApply}
          onClose={onCloseFilterDropdown}
        />
      )}
    </div>
  );
}
