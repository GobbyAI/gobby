import { SettingsSection, type SettingsSectionFields } from "./SettingsSection";
import {
  MapConfigField,
  NumberConfigField,
  SchemaSelectField,
  StringListConfigField,
  Subsection,
  SwitchConfigField,
  TextConfigField,
} from "./configFields";

/**
 * Observability settings section: telemetry, logging, tracing, metrics, and
 * exporters. These are the `telemetry.*` and `metrics.list_limit` keep-rows
 * from the configuration audit, with the two former text-fallback rows
 * (`telemetry.exporter.otlp_headers`, `telemetry.llm_tracing.providers`) fixed
 * into typed editors — a string map and a string list respectively. Every row
 * is a draft-backed DaemonConfig dotted-path, saved through the section shell's
 * Save/Discard footer.
 */

const TELEMETRY_PATHS = [
  "telemetry.service_name",
  "telemetry.log_level",
  "telemetry.log_format",
  "telemetry.log_file",
  "telemetry.log_file_error",
  "telemetry.log_file_hook_manager",
  "telemetry.log_file_mcp_server",
  "telemetry.log_file_mcp_client",
  "telemetry.max_size_mb",
  "telemetry.backup_count",
  "telemetry.traces_enabled",
  "telemetry.traces_to_console",
  "telemetry.trace_sample_rate",
  "telemetry.trace_retention_days",
  "telemetry.metrics_enabled",
  "telemetry.exporter.otlp_endpoint",
  "telemetry.exporter.otlp_protocol",
  "telemetry.exporter.otlp_headers",
  "telemetry.exporter.prometheus_enabled",
  "telemetry.llm_tracing.enabled",
  "telemetry.llm_tracing.capture_content",
  "telemetry.llm_tracing.providers",
] as const;

const LOGGING_PATHS = [
  "logging.level",
  "logging.format",
  "logging.dir",
  "logging.max_size_mb",
  "logging.backup_count",
  "logging.llm_max_size_mb",
  "logging.llm_backup_count",
  "logging.runtime_max_size_mb",
  "logging.growth_warn_mb_per_interval",
] as const;

const METRICS_PATHS = ["metrics.list_limit"] as const;

const OWNED_PATHS: readonly string[] = [
  ...LOGGING_PATHS,
  ...TELEMETRY_PATHS,
  ...METRICS_PATHS,
];

function ServiceGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Service"
      hint="OpenTelemetry resource identity applied to every span, metric, and log."
    >
      <TextConfigField
        fields={fields}
        path="telemetry.service_name"
        label="Service name"
        ariaLabel="Telemetry service name"
        placeholder="gobby-daemon"
      />
    </Subsection>
  );
}

function RuntimeLoggingGroup({
  fields,
}: {
  fields: SettingsSectionFields;
}): JSX.Element {
  return (
    <Subsection
      title="Runtime logging"
      hint="Format, location, rotation, and growth monitoring for Gobby log files."
    >
      <SchemaSelectField
        fields={fields}
        path="logging.level"
        label="Runtime log level"
        ariaLabel="Runtime log level"
      />
      <SchemaSelectField
        fields={fields}
        path="logging.format"
        label="Runtime log format"
        ariaLabel="Runtime log format"
      />
      <TextConfigField
        fields={fields}
        path="logging.dir"
        label="Logs directory"
        ariaLabel="Logs directory"
        placeholder="~/.gobby/logs"
      />
      <NumberConfigField
        fields={fields}
        path="logging.max_size_mb"
        label="Max rotating file size (MB)"
        ariaLabel="Max rotating log file size"
      />
      <NumberConfigField
        fields={fields}
        path="logging.backup_count"
        label="Rotated files to keep"
        ariaLabel="Rotated log files to keep"
      />
      <NumberConfigField
        fields={fields}
        path="logging.llm_max_size_mb"
        label="Max feature LLM log size (MB)"
        ariaLabel="Max feature LLM log file size"
      />
      <NumberConfigField
        fields={fields}
        path="logging.llm_backup_count"
        label="Rotated feature LLM logs to keep"
        ariaLabel="Rotated feature LLM log files to keep"
      />
      <NumberConfigField
        fields={fields}
        path="logging.runtime_max_size_mb"
        label="Max runtime output size (MB)"
        ariaLabel="Max runtime output file size"
      />
      <NumberConfigField
        fields={fields}
        path="logging.growth_warn_mb_per_interval"
        label="Growth warning threshold (MB)"
        ariaLabel="Logs directory growth warning threshold"
      />
    </Subsection>
  );
}

function LoggingGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Logging"
      hint="Levels, format, and rotation for the daemon and its subsystem log files."
    >
      <SchemaSelectField
        fields={fields}
        path="telemetry.log_level"
        label="Log level"
        ariaLabel="Log level"
      />
      <SchemaSelectField
        fields={fields}
        path="telemetry.log_format"
        label="Log format"
        ariaLabel="Log format"
      />
      <TextConfigField
        fields={fields}
        path="telemetry.log_file"
        label="Main log file"
        ariaLabel="Main log file path"
        placeholder="~/.gobby/logs/gobby.log"
      />
      <TextConfigField
        fields={fields}
        path="telemetry.log_file_error"
        label="Error log file"
        ariaLabel="Error log file path"
        placeholder="~/.gobby/logs/gobby-error.log"
      />
      <TextConfigField
        fields={fields}
        path="telemetry.log_file_hook_manager"
        label="Hook manager log file"
        ariaLabel="Hook manager log file path"
        placeholder="~/.gobby/logs/hook-manager.log"
      />
      <TextConfigField
        fields={fields}
        path="telemetry.log_file_mcp_server"
        label="MCP server log file"
        ariaLabel="MCP server log file path"
        placeholder="~/.gobby/logs/mcp-server.log"
      />
      <TextConfigField
        fields={fields}
        path="telemetry.log_file_mcp_client"
        label="MCP client log file"
        ariaLabel="MCP client log file path"
        placeholder="~/.gobby/logs/mcp-client.log"
      />
      <NumberConfigField
        fields={fields}
        path="telemetry.max_size_mb"
        label="Max log file size (MB)"
        ariaLabel="Max log file size"
      />
      <NumberConfigField
        fields={fields}
        path="telemetry.backup_count"
        label="Log backup count"
        ariaLabel="Log backup count"
      />
    </Subsection>
  );
}

function TracingGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Tracing"
      hint="Distributed tracing capture and how long local spans are retained."
    >
      <SwitchConfigField
        fields={fields}
        path="telemetry.traces_enabled"
        label="Enable distributed tracing"
        ariaLabel="Enable distributed tracing"
      />
      <SwitchConfigField
        fields={fields}
        path="telemetry.traces_to_console"
        label="Export spans to console"
        ariaLabel="Export spans to console"
      />
      <NumberConfigField
        fields={fields}
        path="telemetry.trace_sample_rate"
        label="Trace sample rate"
        ariaLabel="Trace sample rate"
        step={0.05}
      />
      <NumberConfigField
        fields={fields}
        path="telemetry.trace_retention_days"
        label="Trace retention (days)"
        ariaLabel="Trace retention days"
      />
    </Subsection>
  );
}

function MetricsGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Metrics"
      hint="Metrics collection and the query bound used by status endpoints."
    >
      <SwitchConfigField
        fields={fields}
        path="telemetry.metrics_enabled"
        label="Enable metrics collection"
        ariaLabel="Enable metrics collection"
      />
      <NumberConfigField
        fields={fields}
        path="metrics.list_limit"
        label="Metrics list limit"
        ariaLabel="Metrics list limit"
      />
    </Subsection>
  );
}

function ExportersGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="Exporters"
      hint="Where traces and metrics are shipped — an OTLP collector and the Prometheus endpoint."
    >
      <TextConfigField
        fields={fields}
        path="telemetry.exporter.otlp_endpoint"
        label="OTLP endpoint"
        ariaLabel="OTLP endpoint"
        placeholder="http://localhost:4317"
        nullable
      />
      <SchemaSelectField
        fields={fields}
        path="telemetry.exporter.otlp_protocol"
        label="OTLP protocol"
        ariaLabel="OTLP protocol"
      />
      <MapConfigField
        fields={fields}
        path="telemetry.exporter.otlp_headers"
        label="OTLP headers"
        ariaLabel="OTLP header"
        keyPlaceholder="header name"
        addLabel="Add header"
      />
      <SwitchConfigField
        fields={fields}
        path="telemetry.exporter.prometheus_enabled"
        label="Enable Prometheus endpoint"
        ariaLabel="Enable Prometheus endpoint"
      />
    </Subsection>
  );
}

function LlmTracingGroup({ fields }: { fields: SettingsSectionFields }) {
  return (
    <Subsection
      title="LLM tracing"
      hint="Auto-instrumentation of LLM calls via OpenLLMetry. Content capture is off by default."
    >
      <SwitchConfigField
        fields={fields}
        path="telemetry.llm_tracing.enabled"
        label="Enable LLM call tracing"
        ariaLabel="Enable LLM call tracing"
      />
      <SwitchConfigField
        fields={fields}
        path="telemetry.llm_tracing.capture_content"
        label="Capture prompt and completion content"
        ariaLabel="Capture LLM content"
      />
      <StringListConfigField
        fields={fields}
        path="telemetry.llm_tracing.providers"
        label="Instrumented providers"
        ariaLabel="Instrumented provider"
        addLabel="Add provider"
        placeholder="anthropic"
      />
    </Subsection>
  );
}

export function ObservabilitySection() {
  return (
    <SettingsSection sectionId="observability" ownedPaths={OWNED_PATHS}>
      {(fields) => (
        <>
          <ServiceGroup fields={fields} />
          <RuntimeLoggingGroup fields={fields} />
          <LoggingGroup fields={fields} />
          <TracingGroup fields={fields} />
          <MetricsGroup fields={fields} />
          <ExportersGroup fields={fields} />
          <LlmTracingGroup fields={fields} />
        </>
      )}
    </SettingsSection>
  );
}
