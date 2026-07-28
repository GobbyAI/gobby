import { useState } from "react";

import { Button } from "../../ui/Button";
import { ActivityPanelSearch } from "../ActivityPanelSearch";
import { CHANNEL_DISPLAY_NAMES, INTEGRATION_CHANNEL_TYPES } from "./channelMetadata";
import {
  type IntegrationFilters,
} from "./IntegrationsTabModel";

interface IntegrationsTabToolbarProps {
  filters: IntegrationFilters;
  onFiltersChange: (filters: IntegrationFilters) => void;
  onAdd: () => void;
}

export function IntegrationsTabToolbar({
  filters,
  onFiltersChange,
  onAdd,
}: IntegrationsTabToolbarProps) {
  const [showFilters, setShowFilters] = useState(false);
  const activeFilterCount =
    (filters.channelType === "all" ? 0 : 1) + (filters.status === "all" ? 0 : 1);

  return (
    <div className="activity-panel-toolbar">
      <ActivityPanelSearch
        value={filters.search}
        onChange={(search) => onFiltersChange({ ...filters, search })}
        placeholder="Search integrations"
        ariaLabel="Search integrations"
      />
      <Button
        type="button"
        variant="accent"
        size="sm"
        className="activity-panel-action-btn activity-filter-button"
        onClick={() => setShowFilters((value) => !value)}
        aria-label="Filter integrations"
        title="Filter integrations"
        aria-expanded={showFilters}
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
      </Button>
      {showFilters && (
        <div className="activity-filter-panel">
          <label className="activity-filter-panel__field">
            <span>Platform</span>
            <select
              aria-label="Platform filter"
              name="integration-platform-filter"
              value={filters.channelType}
              onChange={(event) =>
                onFiltersChange({
                  ...filters,
                  channelType: event.target.value as IntegrationFilters["channelType"],
                })
              }
            >
              <option value="all">All platforms</option>
              {INTEGRATION_CHANNEL_TYPES.map((type) => (
                <option key={type} value={type}>
                  {CHANNEL_DISPLAY_NAMES[type]}
                </option>
              ))}
            </select>
          </label>
          <label className="activity-filter-panel__field">
            <span>Status</span>
            <select
              aria-label="Integration status"
              name="integration-status-filter"
              value={filters.status}
              onChange={(event) =>
                onFiltersChange({
                  ...filters,
                  status: event.target.value as IntegrationFilters["status"],
                })
              }
            >
              <option value="all">All states</option>
              <option value="enabled">Enabled</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
        </div>
      )}
      <Button type="button" variant="accent" size="sm" onClick={onAdd}>
        + Channel
      </Button>
    </div>
  );
}
