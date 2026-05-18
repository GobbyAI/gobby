import type { StageRegistryEntry } from "../../hooks/useStagesRegistry";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { TasksTabFilters } from "./TasksTabFilters";
import type { TaskFilterKey } from "./TasksTabModel";

interface TasksTabToolbarProps {
  search: string;
  onSearchChange: (value: string) => void;
  showFilterDropdown: boolean;
  onToggleFilterDropdown: () => void;
  activeFilterCount: number;
  statusFilters: Set<TaskFilterKey>;
  stagesRegistry: StageRegistryEntry[];
  selectedStageFilters: ReadonlySet<string>;
  onFiltersApply: (filters: Set<TaskFilterKey>, stages: Set<string>) => void;
  onCloseFilterDropdown: () => void;
}

export function TasksTabToolbar({
  search,
  onSearchChange,
  showFilterDropdown,
  onToggleFilterDropdown,
  activeFilterCount,
  statusFilters,
  stagesRegistry,
  selectedStageFilters,
  onFiltersApply,
  onCloseFilterDropdown,
}: TasksTabToolbarProps) {
  return (
    <div className="activity-panel-toolbar">
      <ActivityPanelSearch
        value={search}
        onChange={onSearchChange}
        placeholder="Search"
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
