import type { MouseEvent, ReactNode } from "react";

import { SourceIcon } from "../shared/SourceIcon";
import { SegmentedControl } from "../ui/SegmentedControl";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import type { SessionStatusMode } from "./SessionsTab.entries";
import {
  type WatchingSessionEntry,
  renderBadges,
} from "./SessionsTab.helpers";
import { SessionsFilterDropdown } from "./SessionsFilterDropdown";
import type { SessionsFilters } from "./sessionsFilters";

const SESSION_PROVIDERS: readonly string[] = [
  "claude",
  "codex",
  "droid",
  "gemini",
  "qwen",
];

const STATUS_MODE_OPTIONS = [
  { value: "live" as const, label: "Live" },
  { value: "expired" as const, label: "Expired" },
] as const;

interface SessionsTabToolbarProps {
  activeFilterCount: number;
  filters: SessionsFilters;
  onFiltersChange: (filters: SessionsFilters) => void;
  onSearchChange: (value: string) => void;
  onStatusModeChange: (mode: SessionStatusMode) => void;
  searchInput: string;
  showFilterDropdown: boolean;
  statusMode: SessionStatusMode;
  toggleFilterDropdown: () => void;
  closeFilterDropdown: () => void;
}

export function SessionsTabToolbar({
  activeFilterCount,
  filters,
  onFiltersChange,
  onSearchChange,
  onStatusModeChange,
  searchInput,
  showFilterDropdown,
  statusMode,
  toggleFilterDropdown,
  closeFilterDropdown,
}: SessionsTabToolbarProps) {
  return (
    <div className="activity-panel-toolbar">
      <ActivityPanelSearch
        value={searchInput}
        onChange={onSearchChange}
        placeholder="Search"
      />
      <SegmentedControl<SessionStatusMode>
        value={statusMode}
        onChange={onStatusModeChange}
        options={STATUS_MODE_OPTIONS}
        ariaLabel="Session status filter"
        size="md"
        controlHeight="sm"
        className="activity-panel-toolbar-segmented"
      />
      <button
        type="button"
        className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
        onClick={toggleFilterDropdown}
        title="Filter sessions"
        aria-label="Filter sessions"
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
        <SessionsFilterDropdown
          filters={filters}
          onChange={onFiltersChange}
          providerOptions={SESSION_PROVIDERS}
          onClose={closeFilterDropdown}
        />
      )}
    </div>
  );
}

interface SessionsEntryListProps {
  emptyState: ReactNode;
  entries: WatchingSessionEntry[];
  fetchError: string | null;
  isLoading: boolean;
  onMenuButtonClick: (
    event: MouseEvent<HTMLButtonElement>,
    entry: WatchingSessionEntry,
  ) => void;
  onSelect: (id: string) => void;
  selectedSessionId: string | null;
  topHeight: number;
}

export function SessionsEntryList({
  emptyState,
  entries,
  fetchError,
  isLoading,
  onMenuButtonClick,
  onSelect,
  selectedSessionId,
  topHeight,
}: SessionsEntryListProps) {
  return (
    <div
      className={`overflow-y-auto ${selectedSessionId ? "border-b border-border" : "flex-1"}`}
      style={selectedSessionId ? { height: `${topHeight}%` } : undefined}
    >
      {isLoading && entries.length === 0 ? (
        <ActivityPanelEmpty body="Loading sessions…" />
      ) : fetchError && entries.length === 0 ? (
        <ActivityPanelEmpty body={fetchError} />
      ) : entries.length === 0 ? (
        emptyState
      ) : (
        entries.map((entry) => (
          <SessionEntryRow
            key={`${entry.type}-${entry.id}`}
            entry={entry}
            isSelected={entry.id === selectedSessionId}
            onMenuButtonClick={onMenuButtonClick}
            onSelect={onSelect}
          />
        ))
      )}
    </div>
  );
}

interface SessionEntryRowProps {
  entry: WatchingSessionEntry;
  isSelected: boolean;
  onMenuButtonClick: (
    event: MouseEvent<HTMLButtonElement>,
    entry: WatchingSessionEntry,
  ) => void;
  onSelect: (id: string) => void;
}

function SessionEntryRow({
  entry,
  isSelected,
  onMenuButtonClick,
  onSelect,
}: SessionEntryRowProps) {
  const isPaused = entry.status !== "active";
  const displayLabel = entry.seqNum ? `#${entry.seqNum}: ${entry.label}` : entry.label;

  return (
    <div
      role="button"
      tabIndex={0}
      className={`session-entry${isSelected ? " session-entry--active" : ""}${isPaused ? " session-entry--paused" : ""}`}
      onClick={() => onSelect(entry.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(entry.id);
        }
      }}
    >
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <ActivityRowStatusDot
          kind={
            entry.status === "active"
              ? "active"
              : entry.status === "expired"
                ? "stopped"
                : entry.status === "paused"
                  ? "paused"
                  : "warning"
          }
          pulse={entry.status === "active"}
          label={`Session ${entry.status}`}
        />
        <SourceIcon source={entry.provider} size={14} />
        <span className="activity-row-title">{displayLabel}</span>
      </div>
      <div className="flex items-center gap-1.5">
        {renderBadges(entry)}
        {entry.status !== "expired" && (
          <button
            className="session-more-btn"
            onClick={(event) => onMenuButtonClick(event, entry)}
            title="Session actions"
            aria-label="Session actions"
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="12" cy="5" r="2" />
              <circle cx="12" cy="12" r="2" />
              <circle cx="12" cy="19" r="2" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
