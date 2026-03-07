# OpenTelemetry Logging Migration

## Overview

Replace Gobby's custom logging and metrics infrastructure with OpenTelemetry, using a strangler fig pattern to remove `MetricsCollector`, `ContextLogger`, `ExtraFieldsFormatter`, and related code.

**Scope**: Logs + Metrics. Tracing infrastructure ready but not instrumented (for future fleet observability).

**Backends**: OTLP export + console fallback for local dev.

## Constraints

- Clean break - no backward compatibility period needed
- Must work without external collector (console fallback)
- Tracing hooks ready but not active

## Phase 1: Foundation

**Goal**: Add OTel dependencies and create telemetry module structure.

**Tasks:**
- [ ] Add OpenTelemetry dependencies to pyproject.toml
- [ ] Create src/gobby/telemetry/ module structure
- [ ] Create TelemetrySettings config model (depends: module structure)
- [ ] Implement provider initialization (TracerProvider, MeterProvider, LoggerProvider)

## Phase 2: Logging Migration

**Goal**: Replace custom logging with OTel LoggingHandler.

**Tasks:**
- [ ] Create OTel logging setup function (depends: Phase 1)
- [ ] Update GobbyRunner to use OTel logging init
- [ ] Remove HookManager._setup_logging() method
- [ ] Migrate all get_context_logger() calls to standard logging.getLogger()
- [ ] Delete src/gobby/utils/logging.py

## Phase 3: Metrics Migration

**Goal**: Replace MetricsCollector with OTel MeterProvider.

**Tasks:**
- [ ] Define OTel metrics instruments in telemetry/metrics.py (depends: Phase 1)
- [ ] Migrate HTTP metrics in servers/http.py
- [ ] Migrate MCP metrics in mcp_proxy/manager.py
- [ ] Migrate hook metrics in hooks/hook_manager.py
- [ ] Update /admin/metrics endpoint to serve OTel metrics
- [ ] Delete src/gobby/utils/metrics.py (depends: all migrations complete)

## Phase 4: Config Consolidation

**Goal**: Clean up old config and update daemon config.

**Tasks:**
- [ ] Merge LoggingSettings into TelemetrySettings (depends: Phase 2, Phase 3)
- [ ] Update DaemonConfig to use telemetry config
- [ ] Delete src/gobby/config/logging.py
- [ ] Update CLAUDE.md with new telemetry documentation

## Phase 5: Testing & Verification

**Goal**: Ensure migration is complete and working.

**Tasks:**
- [ ] Add telemetry unit tests
- [ ] Update existing logging/metrics tests
- [ ] Verify console output works without collector
- [ ] Verify OTLP export with local Jaeger
- [ ] Run full test suite

## Files to Delete

| File | Replaced By |
|------|-------------|
| `src/gobby/utils/logging.py` | `src/gobby/telemetry/logging.py` |
| `src/gobby/utils/metrics.py` | `src/gobby/telemetry/metrics.py` |
| `src/gobby/config/logging.py` | `src/gobby/telemetry/config.py` |

## Files to Create

| File | Purpose |
|------|---------|
| `src/gobby/telemetry/__init__.py` | Public API exports |
| `src/gobby/telemetry/config.py` | TelemetrySettings model |
| `src/gobby/telemetry/providers.py` | OTel provider initialization |
| `src/gobby/telemetry/logging.py` | Logging setup with console + OTLP |
| `src/gobby/telemetry/metrics.py` | Pre-defined metric instruments |
| `src/gobby/telemetry/context.py` | Trace context utilities (for future tracing) |

## Files to Modify

| File | Changes |
|------|---------|
| `pyproject.toml` | Add OTel dependencies |
| `src/gobby/runner.py` | Use `init_telemetry()` instead of `setup_file_logging()` |
| `src/gobby/config/app.py` | Replace `logging: LoggingSettings` with `telemetry: TelemetrySettings` |
| `src/gobby/servers/http.py` | Use OTel metrics instead of MetricsCollector |
| `src/gobby/mcp_proxy/manager.py` | Use OTel metrics |
| `src/gobby/hooks/hook_manager.py` | Remove `_setup_logging()`, use OTel metrics |
| `src/gobby/servers/routes/admin.py` | Update `/admin/metrics` endpoint |

## New Dependencies

```toml
dependencies = [
    "opentelemetry-api>=1.22.0",
    "opentelemetry-sdk>=1.22.0",
    "opentelemetry-exporter-otlp-proto-grpc>=1.22.0",
    "opentelemetry-exporter-prometheus>=0.43b0",
    "opentelemetry-instrumentation-logging>=0.43b0",
]
```

## Configuration

```yaml
# ~/.gobby/config.yaml
telemetry:
  service_name: gobby-daemon
  log_level: info
  log_format: text  # or json
  sample_rate: 1.0
  exporter:
    otlp_endpoint: null  # null = console only, set URL for OTLP
    otlp_insecure: true
    prometheus_port: 9464
```

## Verification

1. `uv run gobby start --verbose` - logs appear in console
2. `curl http://localhost:9464/metrics` - Prometheus format metrics
3. With Jaeger: `docker run -p 4317:4317 -p 16686:16686 jaegertracing/all-in-one`
   - Set `otlp_endpoint: http://localhost:4317`
   - Verify logs appear in Jaeger UI
4. `grep -r "MetricsCollector\|ContextLogger\|setup_file_logging" src/` returns nothing
5. `uv run pytest tests/` passes

## Task Mapping

<!-- Updated after task creation via /gobby-plan -->
| Spec Item | Task Ref | Status |
|-----------|----------|--------|
