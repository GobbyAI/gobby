# Ruby Data And Framework Boundaries

## Rails Boundaries

- Keep controllers, views, helpers, models, jobs, mailers, serializers,
  policies, query objects, and services in their proper roles.
- Controllers and jobs should validate inputs, authorize, call domain behavior,
  and translate responses. They should not accumulate domain decisions.
- Models should own persistence invariants and relationships. Avoid turning
  callbacks into hidden workflows.
- Views and templates should format already-prepared data, not query, authorize,
  or mutate state.

## Active Record

- Preserve transaction boundaries, isolation assumptions, lock usage, callbacks,
  validations, scopes, and association loading.
- Check N+1 behavior when changing views, serializers, GraphQL resolvers, or
  collection endpoints.
- Keep migrations reversible where possible and compatible with deploy order.
- Do not make schema, data migration, or enum changes without testing rollback,
  backfill, and old-code/new-code compatibility when deployment can overlap.

## External Boundaries

- Validate and normalize HTTP, queue, file, CLI, webhook, and user input before
  domain code consumes it.
- Wrap external clients behind clear adapters with timeouts, retries, idempotency
  keys, and error translation.
- Keep serialization contracts explicit for JSON, XML, protobuf, Action Cable,
  background jobs, and emails.
- Do not leak Active Record models across package, engine, API, or worker
  boundaries unless the repo already uses that contract.

## Background Work

- Keep jobs idempotent and retry-safe. Avoid passing full AR objects when stable
  IDs or serialized value objects are safer.
- Preserve queue names, priorities, retry policy, uniqueness locks, and
  scheduling behavior.
- Test jobs with the local adapter/helper conventions instead of relying on a
  broad queue run.

## Authorization And Security

- Keep authorization near request/job boundaries and verify it in tests.
- Avoid mass assignment, unsafe `constantize`, string eval, YAML object loading,
  path traversal, SQL fragments, and shell interpolation.
- Use parameterized queries, strong parameters, and framework escaping helpers.
