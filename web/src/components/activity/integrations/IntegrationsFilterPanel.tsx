import {
  InlineFilterFieldRow,
  InlineFilterPanel,
} from "../FilterPrimitives";
import { SelectField } from "../fields";
import { CHANNEL_DISPLAY_NAMES, INTEGRATION_CHANNEL_TYPES } from "./channelMetadata";
import { type IntegrationFilters } from "./IntegrationsTabModel";

interface IntegrationsFilterPanelProps {
  filters: IntegrationFilters;
  onFiltersChange: (filters: IntegrationFilters) => void;
}

/**
 * Filter panel opened by the header Filter trigger (#19159).
 */
export function IntegrationsFilterPanel({
  filters,
  onFiltersChange,
}: IntegrationsFilterPanelProps) {
  return (
    <InlineFilterPanel aria-label="Integration filters">
      <InlineFilterFieldRow>
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
      </InlineFilterFieldRow>
      <InlineFilterFieldRow>
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
      </InlineFilterFieldRow>
    </InlineFilterPanel>
  );
}
