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

const selectClass =
  "min-h-8 rounded-md border border-border bg-[var(--bg-secondary)] px-2 text-xs text-foreground pointer-coarse:min-h-11";

export function IntegrationsTabToolbar({
  filters,
  onFiltersChange,
  onAdd,
}: IntegrationsTabToolbarProps) {
  return (
    <div className="activity-panel-toolbar">
      <ActivityPanelSearch
        value={filters.search}
        onChange={(search) => onFiltersChange({ ...filters, search })}
        placeholder="Search integrations"
        ariaLabel="Search integrations"
      />
      <select
        className={selectClass}
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
      <select
        className={selectClass}
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
      <button type="button" className="btn btn-accent btn-sm" onClick={onAdd}>
        + Channel
      </button>
    </div>
  );
}
