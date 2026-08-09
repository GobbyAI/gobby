import "../../chat/styles/rules-tab.css";
import { SelectField } from "../fields";
import { CHANNEL_DISPLAY_NAMES, INTEGRATION_CHANNEL_TYPES } from "./channelMetadata";
import { type IntegrationFilters } from "./IntegrationsTabModel";

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
      <div className="activity-filter-panel__field">
        <SelectField
          label="Platform"
          ariaLabel="Platform filter"
          name="integration-platform-filter"
          value={filters.channelType}
          onChange={(channelType) =>
            onFiltersChange({
              ...filters,
              channelType: channelType as IntegrationFilters["channelType"],
            })
          }
          options={[
            { value: "all", label: "All platforms" },
            ...INTEGRATION_CHANNEL_TYPES.map((type) => ({
              value: type,
              label: CHANNEL_DISPLAY_NAMES[type],
            })),
          ]}
        />
      </div>
      <div className="activity-filter-panel__field">
        <SelectField
          label="Status"
          ariaLabel="Integration status"
          name="integration-status-filter"
          value={filters.status}
          onChange={(status) =>
            onFiltersChange({
              ...filters,
              status: status as IntegrationFilters["status"],
            })
          }
          options={[
            { value: "all", label: "All states" },
            { value: "enabled", label: "Enabled" },
            { value: "disabled", label: "Disabled" },
          ]}
        />
      </div>
    </div>
  );
}
