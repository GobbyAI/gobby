import { CHANNEL_DISPLAY_NAMES, INTEGRATION_CHANNEL_TYPES } from "./channelMetadata";
import {
  type IntegrationFilters,
} from "./IntegrationsTabModel";

interface IntegrationsFilterPanelProps {
  filters: IntegrationFilters;
  onFiltersChange: (filters: IntegrationFilters) => void;
}

/**
 * Filter panel opened by the header Filter trigger (#19159). Rendered by the
 * tab root, anchored below the header via .activity-filter-panel.
 */
export function IntegrationsFilterPanel({
  filters,
  onFiltersChange,
}: IntegrationsFilterPanelProps) {
  return (
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
  );
}
