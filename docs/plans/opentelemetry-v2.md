# OpenTelemetry Full Observability Stack

## Context

Gobby has custom logging (`utils/logging.py`, 377 lines) and metrics (`utils/metrics.py`, 606 lines) infrastructure that works but doesn't correlate across boundaries. Sessions, agents, pipelines, tool calls, and rule evaluations are all observable individually but can't be traced as a connected flow. The existing `docs/plans/opentelemetry.md` plan focused on logging + metrics migration with tracing as "future work" — this plan supersedes it with a full-stack approach: logs, metrics, and tracing together.

**Goal**: Replace custom logging/metrics with OpenTelemetry, add distributed tracing, migrate ToolMetricsManager to a dual-backend model (OTel for observability + SQLite for queryable analytics), and build a trace viewer into the Gobby web UI.

## Architecture

### New module: `src/gobby/telemetry/`

```
src/gobby/telemetry/
├── __init__.py       # Public API: init_telemetry(), get_tracer(), get_meter(), shutdown_telemetry()
├── config.py         # TelemetrySettings (replaces config/logging.py)
├── providers.py      # TracerProvider, MeterProvider, LoggerProvider init
├── logging.py        # OTel logging bridge (replaces utils/logging.py)
├── metrics.py        # OTel metric instruments (replaces utils/metrics.py MetricsCollector)
├── tracing.py        # @traced decorator, span helpers, async context utils
├── context.py        # ContextVar bridge, trace_id injection, subprocess propagation
├── middleware.py      # FastAPI middleware: request spans + metrics (replaces 20 manual inc_counter calls)
└── exporters.py      # Exporter factory: console, OTLP, Prometheus, rotating-file
```

### Span hierarchy

```
Session root span
├── Hook event span (before_tool, after_tool, etc.)
│   ├── Rule evaluation span (per rule)
│   └── Effect dispatch span
├── Pipeline execution span
│   ├── Step spans (lint, test, deploy, etc.)
│   │   └── Tool call / exec spans
│   └── Webhook notification span
├── Agent spawn span
│   ├── Isolation setup span (worktree/clone)
│   └── Process launch span
└── MCP tool call span
    ├── Server connection span
    └── Tool execution span
```

### ToolMetricsManager migration

Dual-backend: OTel for real-time counters/histograms, SQLite for queryable history.

1. Extract SQLite logic from `ToolMetricsManager` into `ToolMetricsStore` (new file: `mcp_proxy/metrics_store.py`)
2. `ToolMetricsManager.record_call()` writes to both OTel instruments and SQLite
3. All 9+1 `gobby-metrics` MCP tools continue querying SQLite unchanged
4. `ToolFallbackResolver` and discovery endpoint continue reading SQLite unchanged

### Configuration

`TelemetrySettings` replaces `LoggingSettings` in `DaemonConfig`:

```yaml
telemetry:
  service_name: gobby-daemon
  log_level: info
  log_format: text          # or json
  log_file: ~/.gobby/logs/gobby.log
  # ... (preserves all current log file paths + rotation settings)
  traces_enabled: false     # opt-in (local-first, no assumption of external infra)
  traces_to_console: false
  trace_sample_rate: 1.0
  metrics_enabled: true
  exporter:
    otlp_endpoint: null     # null = local-only
    otlp_protocol: grpc
    prometheus_enabled: true
```

### Dependencies

```toml
"opentelemetry-api>=1.28.0"
"opentelemetry-sdk>=1.28.0"
"opentelemetry-exporter-otlp-proto-grpc>=1.28.0"
"opentelemetry-exporter-prometheus>=0.49b0"
"opentelemetry-instrumentation-logging>=0.49b0"
"opentelemetry-semantic-conventions>=0.49b0"
```

No auto-instrumentation for FastAPI/httpx/sqlite — custom middleware gives domain-specific attributes (session_id, project_id, agent_id).

### Span storage (built-in trace viewer)

Spans are persisted to SQLite via a custom `SpanExporter` for the built-in trace viewer. This makes Gobby self-contained — no external Jaeger/Grafana required.

**Backend:**
- `src/gobby/telemetry/span_store.py` — Custom `SpanExporter` that writes completed spans to SQLite `spans` table
- `src/gobby/storage/spans.py` — `SpanStorage` class (CRUD + queries: by trace_id, by session, recent traces, etc.)
- `src/gobby/servers/routes/traces.py` — REST endpoints: `GET /api/traces`, `GET /api/traces/{trace_id}`
- WebSocket event `trace_event` for real-time span updates in the UI
- Retention: 7-day default, cleaned alongside tool metrics in `runner_maintenance.py`

**SQLite schema (`spans` table):**
```sql
CREATE TABLE spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT,                    -- SERVER, CLIENT, INTERNAL, etc.
    start_time_ns INTEGER NOT NULL,
    end_time_ns INTEGER,
    status TEXT,                  -- OK, ERROR, UNSET
    status_message TEXT,
    attributes_json TEXT,         -- {"session_id": "...", "project_id": "...", ...}
    events_json TEXT,             -- [{name, timestamp, attributes}, ...]
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_spans_trace_id ON spans(trace_id);
CREATE INDEX idx_spans_start_time ON spans(start_time_ns);
```

**Frontend:**
- `web/src/components/traces/TracesPage.tsx` — New page: trace list + detail view
- `web/src/components/traces/TraceWaterfall.tsx` — Hand-rolled SVG waterfall/Gantt visualization of spans
- `web/src/components/traces/TraceDetail.tsx` — Span attribute inspector
- `web/src/hooks/useTraces.ts` — Data fetching + WebSocket real-time updates
- `web/src/components/traces/TracesPage.css` — Styling

**UI patterns** (matching existing codebase):
- Tab-based routing via `selectedTab` in `App.tsx` (like all other pages)
- `useWebSocketEvent('trace_event', ...)` for real-time span arrivals
- Native `fetch()` for data, no new libraries
- Hand-rolled SVG waterfall (same approach as existing `GanttChart.tsx`)
- Radix UI primitives for controls (filters, status badges)
- Color-coded spans by status (green=OK, red=ERROR, gray=UNSET)

---

## Phases

### Phase 1: Foundation

Create the telemetry module structure. No behavioral changes.

| Task | Files |
|------|-------|
| Add OTel dependencies | `pyproject.toml` |
| Create `telemetry/config.py` with `TelemetrySettings` | New file |
| Create `telemetry/providers.py` (TracerProvider, MeterProvider, LoggerProvider) | New file |
| Create `telemetry/exporters.py` (exporter factory) | New file |
| Create `telemetry/__init__.py` with `init_telemetry()`, `shutdown_telemetry()` | New file |
| Add `telemetry: TelemetrySettings` to `DaemonConfig` alongside `logging:` | `config/app.py:292` |
| Unit tests for TelemetrySettings, provider creation | New test files |

### Phase 2: Logging migration

Replace custom logging with OTel LoggingHandler + RotatingFileHandler.

| Task | Files |
|------|-------|
| Create `telemetry/logging.py` with `setup_otel_logging()` | New file |
| Create `telemetry/context.py` with `get_trace_id()` (replaces `request_id_var`) | New file |
| Replace `setup_file_logging(verbose)` call in `GobbyRunner.__init__` | `runner.py:71` |
| Delete `HookManager._setup_logging()` (inherits from parent logger) | `hooks/hook_manager.py:227-262` |
| Replace `get_context_logger()` calls with `logging.getLogger()` | Barely used — ~1 test file |
| Delete `ExtraFieldsFormatter`, `RequestIDFilter`, `ContextLogger` | `utils/logging.py` |
| Verify log files still rotate, trace_id appears in logs | Tests |

### Phase 3: Metrics migration

Replace MetricsCollector with OTel instruments + middleware.

| Task | Files |
|------|-------|
| Create `telemetry/metrics.py` with OTel instruments | New file |
| Create `telemetry/middleware.py` (`TelemetryMiddleware`) | New file |
| Add middleware to FastAPI app | `servers/http.py` |
| Migrate 20 route files off `get_metrics_collector()` / `inc_counter()` | 20 files in `servers/routes/` |
| Update `GET /admin/metrics` to use `prometheus_client.generate_latest()` | `servers/routes/admin/_health.py` |
| Extract `ToolMetricsStore` from `ToolMetricsManager` | New: `mcp_proxy/metrics_store.py` |
| Add OTel dual-write to `ToolMetricsManager.record_call()` | `mcp_proxy/metrics.py` |
| Delete `utils/metrics.py` | After all consumers migrated |
| Verify Prometheus output preserved, middleware captures all routes | Tests |

### Phase 4: Tracing

Add span instrumentation across key flows.

| Task | Files |
|------|-------|
| Create `telemetry/tracing.py` with `@traced` decorator, span helpers | New file |
| Instrument `MCPClientManager.call_tool()` with spans | `mcp_proxy/manager.py:636` |
| Instrument `PipelineExecutor.execute()` + `_execute_step()` | `workflows/pipeline_executor.py` |
| Instrument `HookManager.handle()` | `hooks/hook_manager.py` |
| Instrument `RuleEngine.evaluate()` | `workflows/rule_engine.py` |
| Instrument `AgentRunner` spawn with context injection via env vars | `agents/runner.py` |
| Add `inject_into_env()` / `extract_from_env()` to `context.py` | `telemetry/context.py` |
| Verify span hierarchy, context propagation across async boundaries | Tests |

### Phase 5: Span storage + API

Persist spans to SQLite and expose REST/WebSocket endpoints for the trace viewer.

| Task | Files |
|------|-------|
| Add `spans` table migration | `storage/migrations.py` |
| Create `storage/spans.py` with `SpanStorage` (query by trace_id, session, recent) | New file |
| Create `telemetry/span_store.py` with custom `SpanExporter` writing to SQLite | New file |
| Wire `SpanExporter` into `TracerProvider` in `providers.py` | `telemetry/providers.py` |
| Create `servers/routes/traces.py` with `GET /api/traces`, `GET /api/traces/{trace_id}` | New file |
| Register trace routes in HTTP server | `servers/http.py` |
| Add `trace_event` WebSocket broadcast on span export | `telemetry/span_store.py` + `servers/websocket/` |
| Add span retention cleanup to maintenance loop | `runner_maintenance.py` |
| Tests for SpanStorage, SpanExporter, trace API endpoints | New test files |

### Phase 6: Trace viewer UI

Build the trace visualization page in the web UI.

| Task | Files |
|------|-------|
| Create `useTraces.ts` hook (fetch + WebSocket subscription) | `web/src/hooks/useTraces.ts` |
| Create `TracesPage.tsx` (trace list with filters) | `web/src/components/traces/TracesPage.tsx` |
| Create `TraceWaterfall.tsx` (SVG span waterfall visualization) | `web/src/components/traces/TraceWaterfall.tsx` |
| Create `TraceDetail.tsx` (span attribute/event inspector) | `web/src/components/traces/TraceDetail.tsx` |
| Add `TracesPage.css` styling | `web/src/components/traces/TracesPage.css` |
| Register "Traces" tab in `App.tsx` | `web/src/App.tsx` |
| Add trace link integration to pipeline execution view | `web/src/components/workflows/PipelineExecutionsView.tsx` |
| Verify end-to-end: trigger pipeline, see spans appear in real-time | Manual test |

### Phase 7: Config consolidation + cleanup

| Task | Files |
|------|-------|
| Replace `logging: LoggingSettings` with `telemetry: TelemetrySettings` in `DaemonConfig` | `config/app.py:292` |
| Delete `config/logging.py` | `config/logging.py` (71 lines) |
| Delete `utils/logging.py` entirely | `utils/logging.py` (377 lines) |
| Delete `utils/metrics.py` entirely | `utils/metrics.py` (606 lines) |
| Remove `LoggingSettings` import from `config/app.py` | `config/app.py:42` |
| Delete `docs/plans/opentelemetry.md` (superseded) | `docs/plans/opentelemetry.md` |
| Update test fixtures for new telemetry setup | `tests/conftest.py` + related |

---

## Files deleted (total: ~1,054 lines removed)

| File | Lines | Replaced by |
|------|-------|-------------|
| `src/gobby/utils/logging.py` | 377 | `telemetry/logging.py` + `telemetry/context.py` |
| `src/gobby/utils/metrics.py` | 606 | `telemetry/metrics.py` + `telemetry/middleware.py` |
| `src/gobby/config/logging.py` | 71 | `telemetry/config.py` |

## Files created

| File | Purpose |
|------|---------|
| `src/gobby/telemetry/__init__.py` | Public API |
| `src/gobby/telemetry/config.py` | TelemetrySettings model |
| `src/gobby/telemetry/providers.py` | OTel provider init |
| `src/gobby/telemetry/exporters.py` | Exporter factory |
| `src/gobby/telemetry/logging.py` | Logging bridge |
| `src/gobby/telemetry/metrics.py` | Metric instruments |
| `src/gobby/telemetry/tracing.py` | Span helpers + decorator |
| `src/gobby/telemetry/context.py` | Context propagation |
| `src/gobby/telemetry/middleware.py` | FastAPI middleware |
| `src/gobby/telemetry/span_store.py` | Custom SpanExporter → SQLite |
| `src/gobby/mcp_proxy/metrics_store.py` | SQLite query layer (extracted from ToolMetricsManager) |
| `src/gobby/storage/spans.py` | SpanStorage (query traces from SQLite) |
| `src/gobby/servers/routes/traces.py` | Trace REST API endpoints |
| `web/src/hooks/useTraces.ts` | Trace data fetching + WebSocket |
| `web/src/components/traces/TracesPage.tsx` | Trace list + detail page |
| `web/src/components/traces/TraceWaterfall.tsx` | SVG waterfall visualization |
| `web/src/components/traces/TraceDetail.tsx` | Span attribute inspector |
| `web/src/components/traces/TracesPage.css` | Trace page styling |

## Key files modified

| File | Change |
|------|--------|
| `pyproject.toml` | Add 6 OTel dependencies |
| `src/gobby/runner.py:71` | `setup_file_logging()` → `init_telemetry()` |
| `src/gobby/config/app.py:292` | `logging: LoggingSettings` → `telemetry: TelemetrySettings` |
| `src/gobby/hooks/hook_manager.py:227-262` | Delete `_setup_logging()`, add span instrumentation |
| `src/gobby/mcp_proxy/manager.py:636` | Add span wrapping in `call_tool()` |
| `src/gobby/mcp_proxy/metrics.py` | Refactor to facade over OTel + SQLite store |
| `src/gobby/servers/http.py` | Add TelemetryMiddleware, remove shutdown metrics |
| `src/gobby/servers/routes/admin/_health.py` | Use `prometheus_client.generate_latest()` |
| `src/gobby/workflows/pipeline_executor.py` | Add step-level spans |
| `src/gobby/workflows/rule_engine.py` | Add rule evaluation spans |
| `src/gobby/agents/runner.py` | Add context injection to subprocess env |
| 20 route files in `servers/routes/` | Remove `get_metrics_collector()` imports + manual counter calls |
| `src/gobby/storage/migrations.py` | Add `spans` table migration |
| `src/gobby/runner_maintenance.py` | Add span retention cleanup |
| `web/src/App.tsx` | Register Traces tab |
| `web/src/components/workflows/PipelineExecutionsView.tsx` | Add trace link to pipeline executions |

## Key decisions

1. **Tracing is opt-in** (`traces_enabled: false`). Local-first means no assumption of external infra. Logging + metrics work without a collector.
2. **Custom middleware over auto-instrumentation**. Gobby needs domain-specific span attributes (session_id, project_id, agent_id) that generic auto-instrumentors don't provide.
3. **ToolMetricsManager becomes a facade**, not replaced. SQLite stays for queryable analytics; OTel handles real-time observability. The 10 MCP tools keep working unchanged.
4. **request_id_var → trace_id**. OTel trace context replaces the custom ContextVar. `get_trace_id()` in `context.py` provides the migration bridge.
5. **PrometheusMetricReader for /admin/metrics**. Uses `opentelemetry-exporter-prometheus` which integrates with `prometheus_client`. Preserves the existing endpoint contract.
6. **Clean break for config**. The `logging:` key is removed from `DaemonConfig`. No deprecation validator, no migration shim. Old configs with `logging:` will fail loudly on load — users update their YAML.

## Verification

1. **Logging**: `uv run gobby start --verbose` — logs appear in `~/.gobby/logs/gobby.log` with `trace_id=` fields
2. **Metrics**: `curl http://localhost:60887/admin/metrics` — Prometheus text format output with all current metric names
3. **Tracing**: Trigger a tool call, verify span written to SQLite `spans` table
4. **Trace API**: `curl http://localhost:60887/api/traces` — returns recent traces as JSON
5. **Trace detail**: `curl http://localhost:60887/api/traces/{trace_id}` — returns full span tree
6. **Trace viewer UI**: Open web UI → Traces tab → see waterfall visualization of spans with correct nesting and timing
7. **Real-time updates**: Trigger a pipeline execution, watch spans appear in the trace viewer via WebSocket
8. **Pipeline integration**: Pipeline execution view links to its trace in the trace viewer
9. **With collector** (optional): Set `otlp_endpoint`, verify spans also export to Jaeger/Grafana
10. **MCP tools**: `gobby-metrics` tools (`get_top_tools`, `get_failing_tools`, etc.) — all return same data as before
11. **No remnants**: `grep -r "MetricsCollector\|ContextLogger\|setup_file_logging\|ExtraFieldsFormatter" src/` returns nothing
12. **Tests**: `uv run pytest tests/telemetry/ tests/mcp_proxy/test_metrics_manager.py tests/storage/test_spans.py -v` — all pass
13. **Config clean break**: Config with old `logging:` key fails to load (Pydantic validation error)
