# Elixir Data And Boundaries

## Phoenix And Contexts

- Keep controllers, channels, LiveViews, components, contexts, schemas, and
  domain modules separated according to the repo's structure.
- Controllers and LiveViews should validate request/UI concerns, call contexts,
  and render results. Domain rules belong in contexts or domain modules.
- Do not bury HTTP, filesystem, database, or process side effects in helpers that
  look pure.

## Ecto

- Keep changesets explicit about required fields, constraints, casts, embeds,
  associations, and validations.
- Use `Ecto.Multi` for multi-step transactions that need atomicity and clear
  rollback reasons.
- Avoid N+1 queries. Preload deliberately and keep query ownership in the
  context or repository layer used by the repo.
- Migrations must be reversible when practical, deploy-safe, and compatible with
  existing data.

## External Boundaries

- Isolate HTTP clients, queues, mailers, filesystem, cloud APIs, and process
  boundaries behind modules that can be tested.
- Normalize external data at the edge. Keep raw external payloads out of domain
  internals unless the contract requires them.
- Preserve idempotency keys, retries, timeout behavior, and audit fields.

## Templates And LiveView

- Keep assigns complete and minimal. Avoid hidden dependencies on process
  dictionary, globals, or implicit socket state.
- Validate events and params before changing state.
- Keep HEEx/EEx changes paired with component tests or render assertions when
  behavior changes.
