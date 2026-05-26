# LangWatch Integration for Gobby

## Context

Gobby has no per-LLM-call observability. Session-level token/cost aggregation exists (`sessions/token_tracker.py`), but you can't see individual LLM calls, their latency, token usage, or content. When an agent misbehaves or a pipeline produces bad output, there's no way to inspect what the LLM actually did.

LangWatch is an open-source LLMOps platform (tracing, evaluation, monitoring). It's **OpenTelemetry-native** — it accepts standard OTLP traces. This means LangWatch is just an OTLP endpoint, not a proprietary SDK dependency.

Gobby already has a detailed OTel v2 plan (`docs/plans/opentelemetry-v2.md`) covering logging, metrics, tracing, span storage, and a trace viewer UI — but none of it is implemented yet. The OTel v2 plan explicitly omits LLM auto-instrumentation.

**What LangWatch adds that OTel v2 doesn't cover:**
- LLM call auto-instrumentors (via [OpenLLMetry](https://github.com/traceloop/openllmetry)) that capture model, tokens, latency, cost, and optionally prompt/completion content
- An LLM-focused dashboard with cost analytics, evaluation framework (20+ built-in evaluators), and agent workflow visualization
- Self-hostable (Docker/K8s) or SaaS

**What LangWatch does NOT add:**
- It's not a new tracing system — it consumes OTel spans
- No proprietary SDK needed — standard OTLP export
- No code coupling — LangWatch is a config-level integration (endpoint URL + API key)

## Approach

LangWatch integration = OTel v2 Phase 1 (Foundation) + LLM auto-instrumentors + OTLP export config. No `langwatch` Python package. No `@langwatch.trace()` decorators. Pure OTel with OpenLLMetry instrumentors.

### What to build

**1. Add OpenLLMetry auto-instrumentors to OTel v2 plan**

New file: `src/gobby/telemetry/instrumentors.py` (~80 lines)

Activates OpenLLMetry auto-instrumentors for the three LLM SDKs Gobby uses directly:
- `opentelemetry-instrumentation-anthropic` — captures `anthropic.Anthropic.messages.create()` calls
- `opentelemetry-instrumentation-openai` — captures OpenAI/Azure calls (used by codex.py, litellm.py)
- `opentelemetry-instrumentation-google-genai` — captures Gemini calls

These monkey-patch the SDK clients to emit OTel spans with [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/): `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens`, etc.

Must be called **before** any LLM client is instantiated (i.e., before `create_llm_service()` in `runner.py`).

Key config: `capture_content: bool = False` — controls whether prompt/completion text is included in spans. Default off (privacy-first, local-first).

**Limitation:** Subscription-mode Claude (CLI subprocess) is opaque — auto-instrumentors can't see into it. Only `api_key`/`adc` auth modes produce LLM spans. This is fine.

**2. Add OTLP/HTTP export support to OTel v2 exporter factory**

The OTel v2 plan's `exporters.py` already plans for OTLP export but specifies gRPC. LangWatch uses OTLP/HTTP. Support both protocols:

```yaml
telemetry:
  traces_enabled: true
  exporter:
    otlp_endpoint: "https://app.langwatch.ai/api/otel/v1/traces"  # or self-hosted
    otlp_protocol: http  # or grpc
    otlp_headers:
      Authorization: "Bearer $LANGWATCH_API_KEY"
  llm_tracing:
    enabled: true
    capture_content: false
    providers: [anthropic, openai, google-genai]
```

Add `llm_tracing` section to `TelemetrySettings` in `telemetry/config.py`.

**3. Enrich LLM spans with Gobby context via `GobbySpanProcessor`**

The OTel v2 plan already defines `telemetry/context.py` for ContextVar-based context propagation. Extend it with a custom `SpanProcessor` that injects Gobby domain context into every span:

- `gobby.session_id` — from `session_id_var` ContextVar
- `gobby.task_id` — from `task_id_var` ContextVar
- `gobby.agent_id` — from `agent_id_var` ContextVar
- `gobby.project_id` — from `project_id_var` ContextVar
- `gobby.pipeline_id` — from `pipeline_id_var` ContextVar

Set these ContextVars at boundary points:
- `agents/runner.py` — set agent_id + session_id before executor.run()
- `workflows/pipeline_executor.py` — set pipeline_id before execute()
- `hooks/hook_manager.py` — set session_id + project_id at hook entry
- `mcp_proxy/server.py` — set session_id from MCP request context

This is ~3 lines of code at each boundary point. No signature changes.

**4. Optional deps in `pyproject.toml`**

```toml
[project.optional-dependencies]
llm-tracing = [
    "opentelemetry-instrumentation-anthropic>=0.34.0",
    "opentelemetry-instrumentation-openai>=0.34.0",
    "opentelemetry-instrumentation-google-genai>=0.34.0",
]
```

The core OTel deps (`opentelemetry-api`, `opentelemetry-sdk`, etc.) go in the main deps as the OTel v2 plan specifies. The LLM instrumentors are optional because they're only useful if you want LLM-level tracing.

### What NOT to build

- No `langwatch` Python package dependency
- No `@langwatch.trace()` / `@langwatch.span()` decorators (vendor lock-in)
- No LangWatch-specific module or adapter
- No LangWatch evaluator integration (that's a separate feature if we want it later)
- No changes to existing metrics (Prometheus, ToolMetricsManager)

### Dependency on OTel v2

This work requires OTel v2 Phase 1 (Foundation) at minimum:
- `telemetry/__init__.py` with `init_telemetry()`
- `telemetry/config.py` with `TelemetrySettings`
- `telemetry/providers.py` with `TracerProvider` setup
- `telemetry/exporters.py` with exporter factory

The LLM instrumentor and context propagation pieces can be built in parallel with OTel v2 Phases 2-4 and merged once Phase 1 lands.

### Data flow

```
provider.generate_text() → anthropic.messages.create()
    │
    ├── [AnthropicInstrumentor] emits OTel span:
    │     gen_ai.system=anthropic, gen_ai.request.model=claude-sonnet-4-6
    │     gen_ai.usage.prompt_tokens=1200, gen_ai.usage.completion_tokens=340
    │     (if capture_content: gen_ai.prompt, gen_ai.completion)
    │
    ├── [GobbySpanProcessor] enriches span:
    │     gobby.session_id=#1234, gobby.agent_id=agent-abc
    │
    └── [BatchSpanProcessor] exports to:
          ├── SqliteSpanExporter → local trace viewer (always)
          └── OTLPSpanExporter → LangWatch (if configured)
```

### Files to create/modify

| File | Action | Purpose |
|------|--------|---------|
| `src/gobby/telemetry/instrumentors.py` | Create | LLM auto-instrumentor activation |
| `src/gobby/telemetry/config.py` | Modify (OTel v2) | Add `LLMTracingConfig` to `TelemetrySettings` |
| `src/gobby/telemetry/context.py` | Modify (OTel v2) | Add `GobbySpanProcessor` + ContextVars |
| `src/gobby/telemetry/exporters.py` | Modify (OTel v2) | Add OTLP/HTTP protocol support |
| `src/gobby/runner.py` | Modify | Call `setup_llm_instrumentors()` before `create_llm_service()` |
| `src/gobby/agents/runner.py` | Modify | Set agent_id/session_id ContextVars (~3 lines) |
| `src/gobby/workflows/pipeline_executor.py` | Modify | Set pipeline_id ContextVar (~2 lines) |
| `src/gobby/hooks/hook_manager.py` | Modify | Set session_id/project_id ContextVars (~3 lines) |
| `pyproject.toml` | Modify | Add `llm-tracing` optional deps |
| `tests/telemetry/test_instrumentors.py` | Create | Test instrumentor activation + graceful no-op |
| `tests/telemetry/test_context.py` | Create | Test GobbySpanProcessor context injection |

### Verification

1. `uv sync --extra llm-tracing` installs OpenLLMetry instrumentors
2. Configure `telemetry.llm_tracing.enabled: true` in daemon config
3. Make an LLM call (e.g., via pipeline prompt step or agent execution)
4. Check local PostgreSQL `spans` table — LLM call span exists with `gen_ai.*` attributes + `gobby.*` context
5. Configure `telemetry.exporter.otlp_endpoint` to a LangWatch instance
6. Make another LLM call — span appears in LangWatch dashboard with Gobby context
7. Verify `capture_content: false` (default) does NOT send prompt/completion text
8. Verify `capture_content: true` DOES send prompt/completion text
9. Verify instrumentors gracefully no-op when `llm-tracing` extras not installed
