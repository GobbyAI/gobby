# Elixir Performance And Releases

## Measuring BEAM Work

- Measure before optimizing. Use repo tooling, Telemetry, Benchee, Erlang
  observer tools, `:timer.tc`, tracing, or production metrics as appropriate.
- Check reductions, scheduler utilization, mailbox growth, process count, memory,
  binary retention, ETS pressure, database time, and external IO.
- Keep readability and correctness first for non-hot paths.

## Common Hot Spots

- Avoid building large intermediate lists when streams, chunking, or database
  pagination fit the workload.
- Watch binary retention when slicing or storing large binaries.
- Use ETS, persistent_term, pooling, batching, or partitioned processes only
  when evidence shows the tradeoff is worth it.
- Fix N+1 queries and missing preloads before optimizing Elixir loops around
  database access.

## Releases And Deployment

- Keep release config, config providers, runtime secrets, migrations, startup,
  shutdown, clustering, and health checks compatible with deployment.
- Avoid compile-time env for runtime deployment settings.
- Keep release tasks idempotent and safe to run in expected environments.
- Test runtime config and release behavior when config, supervision, or startup
  code changes.

## Operational Safety

- Preserve graceful shutdown and backpressure.
- Do not start unsupervised long-lived processes during application boot.
- Keep telemetry and logs stable enough for dashboards and alerts that depend on
  event names or metadata.
