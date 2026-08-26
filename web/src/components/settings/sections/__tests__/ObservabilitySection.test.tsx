import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ObservabilitySection } from "../ObservabilitySection";
import {
  SettingsSectionContext,
  type SettingsSectionContextValue,
} from "../SettingsSectionContext";

// Schema covering the rows the assertions touch. The three enum selects
// (`telemetry.log_level`, `telemetry.log_format`, `telemetry.exporter.otlp_protocol`)
// prove nested `$ref` traversal through the real DaemonConfig shape — the
// protocol enum in particular resolves two hops deep (telemetry -> exporter).
// `otlp_headers` is the map "fix" row and `llm_tracing.providers` the array
// "fix" row from the configuration audit. `LoggingSettings` carries only the
// feature-LLM rotation rows so the runtime logging group is bound to values.
const SCHEMA: Record<string, unknown> = {
  $defs: {
    LoggingSettings: {
      type: "object",
      properties: {
        llm_max_size_mb: { type: "integer" },
        llm_backup_count: { type: "integer" },
      },
    },
    ExporterSettings: {
      type: "object",
      properties: {
        otlp_endpoint: { type: ["string", "null"] },
        otlp_protocol: { enum: ["grpc", "http"], type: "string" },
        otlp_headers: {
          type: "object",
          additionalProperties: { type: "string" },
        },
        prometheus_enabled: { type: "boolean" },
      },
    },
    LLMTracingConfig: {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        capture_content: { type: "boolean" },
        providers: { type: "array", items: { type: "string" } },
      },
    },
    TelemetrySettings: {
      type: "object",
      properties: {
        service_name: { type: "string" },
        log_level: {
          enum: ["debug", "info", "warning", "error"],
          type: "string",
        },
        log_format: { enum: ["text", "json"], type: "string" },
        max_size_mb: { type: "integer" },
        backup_count: { type: "integer" },
        trace_sample_rate: { type: "number" },
        trace_retention_days: { type: "integer", exclusiveMinimum: 0 },
        exporter: { $ref: "#/$defs/ExporterSettings" },
        llm_tracing: { $ref: "#/$defs/LLMTracingConfig" },
      },
    },
    MetricsConfig: {
      type: "object",
      properties: {
        list_limit: { type: "integer" },
      },
    },
  },
  type: "object",
  properties: {
    logging: { $ref: "#/$defs/LoggingSettings" },
    telemetry: { $ref: "#/$defs/TelemetrySettings" },
    metrics: { $ref: "#/$defs/MetricsConfig" },
  },
};

function makeConfigValues(): Record<string, unknown> {
  return {
    logging: {
      llm_max_size_mb: 50,
      llm_backup_count: 5,
    },
    telemetry: {
      service_name: "gobby-daemon",
      log_level: "info",
      log_format: "text",
      log_file: "~/.gobby/logs/gobby.log",
      log_file_error: "~/.gobby/logs/gobby-error.log",
      log_file_hook_manager: "~/.gobby/logs/hook-manager.log",
      log_file_mcp_server: "~/.gobby/logs/mcp-server.log",
      log_file_mcp_client: "~/.gobby/logs/mcp-client.log",
      max_size_mb: 10,
      backup_count: 5,
      traces_enabled: true,
      traces_to_console: false,
      trace_sample_rate: 1,
      trace_retention_days: 7,
      metrics_enabled: true,
      exporter: {
        otlp_endpoint: "http://localhost:4317",
        otlp_protocol: "grpc",
        otlp_headers: { Authorization: "Bearer token" },
        prometheus_enabled: true,
      },
      llm_tracing: {
        enabled: false,
        capture_content: false,
        providers: ["anthropic", "openai"],
      },
    },
    metrics: {
      list_limit: 10000,
    },
  };
}

function makeContext(
  overrides: Partial<SettingsSectionContextValue> = {},
): SettingsSectionContextValue {
  return {
    schema: SCHEMA,
    configValues: makeConfigValues(),
    secretKeys: [],
    isLoading: false,
    saveConfig: vi.fn(async () => ({ ok: true })),
    registerDirtyGuard: () => () => {},
    ...overrides,
  };
}

function renderSection(ctx: SettingsSectionContextValue) {
  return render(
    <SettingsSectionContext.Provider value={ctx}>
      <ObservabilitySection />
    </SettingsSectionContext.Provider>,
  );
}

describe("ObservabilitySection", () => {
  it("renders the telemetry service identity and logging scalars", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("Telemetry service name")).toHaveValue(
      "gobby-daemon",
    );
    expect(screen.getByLabelText("Main log file path")).toHaveValue(
      "~/.gobby/logs/gobby.log",
    );
    expect(screen.getByLabelText("MCP client log file path")).toHaveValue(
      "~/.gobby/logs/mcp-client.log",
    );
    expect(screen.getByLabelText("Max log file size")).toHaveValue(10);
    expect(screen.getByLabelText("Log backup count")).toHaveValue(5);
  });

  it("renders the feature LLM log rotation rows bound to the draft", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    expect(screen.getByLabelText("Max feature LLM log file size")).toHaveValue(
      50,
    );
    expect(
      screen.getByLabelText("Rotated feature LLM log files to keep"),
    ).toHaveValue(5);

    fireEvent.change(screen.getByLabelText("Max feature LLM log file size"), {
      target: { value: "80" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "logging.llm_max_size_mb": 80 }),
    );
  });

  it("renders log level and format as schema-backed enum selects", () => {
    renderSection(makeContext());

    const level = screen.getByLabelText("Log level");
    expect(level).toHaveValue("info");
    expect(within(level).getAllByRole("option")).toHaveLength(4);

    const format = screen.getByLabelText("Log format");
    expect(format).toHaveValue("text");
    expect(within(format).getAllByRole("option")).toHaveLength(2);
  });

  it("renders tracing controls bound to the draft", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable distributed tracing" }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", { name: "Export spans to console" }),
    ).not.toBeChecked();
    expect(screen.getByLabelText("Trace sample rate")).toHaveValue(1);
    expect(screen.getByLabelText("Trace retention days")).toHaveValue(7);
  });

  it("renders metrics controls including the list limit", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable metrics collection" }),
    ).toBeChecked();
    expect(screen.getByLabelText("Metrics list limit")).toHaveValue(10000);
  });

  it("resolves the OTLP protocol enum two hops deep through $ref", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("OTLP endpoint")).toHaveValue(
      "http://localhost:4317",
    );
    const protocol = screen.getByLabelText("OTLP protocol");
    expect(protocol).toHaveValue("grpc");
    expect(within(protocol).getAllByRole("option")).toHaveLength(2);
    expect(
      screen.getByRole("switch", { name: "Enable Prometheus endpoint" }),
    ).toBeChecked();
  });

  it("renders OTLP headers as a key/value map editor", () => {
    renderSection(makeContext());

    expect(screen.getByLabelText("OTLP header key 1")).toHaveValue(
      "Authorization",
    );
    expect(screen.getByLabelText("Value for Authorization")).toHaveValue(
      "Bearer token",
    );
  });

  it("renders LLM tracing controls and the provider list", () => {
    renderSection(makeContext());

    expect(
      screen.getByRole("switch", { name: "Enable LLM call tracing" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("switch", { name: "Capture LLM content" }),
    ).not.toBeChecked();
    expect(screen.getByLabelText("Instrumented provider item 1")).toHaveValue(
      "anthropic",
    );
    expect(screen.getByLabelText("Instrumented provider item 2")).toHaveValue(
      "openai",
    );
  });

  it("persists an edited draft row through the section Save", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.click(
      screen.getByRole("switch", { name: "Enable distributed tracing" }),
    );
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "telemetry.traces_enabled": false }),
    );
  });

  it("clears the OTLP endpoint to null when emptied", async () => {
    const ctx = makeContext();
    renderSection(ctx);

    fireEvent.change(screen.getByLabelText("OTLP endpoint"), {
      target: { value: "" },
    });
    const save = screen.getByRole("button", { name: "Save" });
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(ctx.saveConfig).toHaveBeenCalledTimes(1));
    expect(ctx.saveConfig).toHaveBeenCalledWith(
      expect.objectContaining({ "telemetry.exporter.otlp_endpoint": null }),
    );
  });
});
