# Gobby Log Collector Reference

[`gobby-logs.yaml`](gobby-logs.yaml) is tested with
`otel/opentelemetry-collector-contrib:0.156.0`. It tails Gobby's eight log
surfaces and sends them to an OTLP/HTTP backend. The collector remains an
operator-managed process; Gobby does not start or supervise it.

## Validate And Run

Create a durable checkpoint directory, then validate the configuration with the
pinned collector image:

```bash
mkdir -p "$HOME/.gobby/otelcol"
docker run --rm \
  -e GOBBY_LOG_DIR=/var/log/gobby \
  -e GOBBY_OTEL_STORAGE_DIR=/var/lib/otelcol/gobby \
  -e GOBBY_OTLP_ENDPOINT=http://host.docker.internal:4318 \
  -v "$HOME/.gobby/logs:/var/log/gobby:ro" \
  -v "$HOME/.gobby/otelcol:/var/lib/otelcol/gobby" \
  -v "$PWD/docs/examples/otel-collector/gobby-logs.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  otel/opentelemetry-collector-contrib:0.156.0 \
  validate --config=/etc/otelcol-contrib/config.yaml
```

Remove `validate` to run the collector. Replace
`GOBBY_OTLP_ENDPOINT` with the base URL of the backend's OTLP/HTTP endpoint.
The canonical collector exporter name is `otlp_http`; `otlphttp` is its
deprecated alias in `0.156.0`.

For Loki 3.x with native OTLP ingestion, point the same exporter at Loki's
`/otlp` base path:

```bash
GOBBY_OTLP_ENDPOINT=http://loki:3100/otlp
```

The exporter appends `/v1/logs`, producing
`http://loki:3100/otlp/v1/logs`. Backend authentication and TLS belong in an
operator-owned copy or overlay of this reference configuration.

## Collection Semantics

The receivers expose `log.file.name`, `log.file.path`, and a bounded
`gobby.log.surface` attribute. The possible surface values are `daemon`,
`errors`, `runtime`, `hooks`, `mcp`, `automation`, `ui`, and `parser`.

Formatted Python logs accept either JSON records or timestamp-led text records.
Valid JSON becomes a structured OTLP body. A malformed JSON-looking record is
forwarded as raw text. Text exceptions are recombined with their timestamp-led
first line. `runtime.log` and `ui.log` remain raw. Parser records are recombined
from their timestamp header through all following payload lines.

Every receiver uses `start_at: end`. A newly seen file starts at its current
end, avoiding first-start replay of historical data and potentially sensitive
parser payloads. The `file_storage` extension persists offsets after that first
observation, so restarts resume from checkpoints. Keep its directory on durable
storage and restrict access to the same operator account that can read Gobby's
logs.

Receiver retries and the exporter's persistent sending queue provide
at-least-once delivery. Duplicates remain possible. In particular,
`on_truncate: read_whole_file` protects Gobby's in-place `runtime.log`
truncation path from losing newly written bytes by rereading the truncated file;
that reread can replay records already exported. Backends should tolerate or
deduplicate repeated records.

Gobby's `telemetry.exporters.otlp_endpoint` configures the daemon's in-process
span exporter. It does not configure these filelog receivers or the collector's
backend exporter; the collector uses `GOBBY_OTLP_ENDPOINT` above.

On Windows, run `otelcol-contrib.exe` directly and set the three environment
variables to absolute Windows paths. Use forward slashes in `GOBBY_LOG_DIR`
(for example, `C:/Users/name/.gobby/logs`) so the receiver glob suffixes remain
portable. The checkpoint directory must already exist or be creatable by the
collector service account.
