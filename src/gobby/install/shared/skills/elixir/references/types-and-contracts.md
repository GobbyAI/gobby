# Elixir Types And Contracts

## Module Contracts

- Treat public functions, behaviours, callbacks, protocols, schemas, contexts,
  and message formats as contracts.
- Preserve return shapes such as `{:ok, value}`, `{:error, reason}`, structs,
  maps, keyword lists, and streams unless the API change is intentional.
- Keep function heads, guards, and pattern matches aligned with supported inputs.
  Avoid catch-all clauses that hide unsupported data.

## Typespecs And Dialyzer

- Add or update `@type`, `@opaque`, `@typedoc`, `@spec`, and callback specs when
  public data or return shapes change.
- Keep opaque types opaque outside their owning module.
- Prefer structured reason types over vague `term()` when errors cross module
  boundaries.
- Fix Dialyzer complaints by making code and specs agree. Avoid weakening specs
  to silence analysis.

## Structs, Schemas, And Protocols

- Use structs for domain data with known shape. Avoid bare maps when fields are
  part of a contract.
- Keep Ecto schema fields, embedded schemas, changeset constraints, and preload
  expectations explicit.
- Implement protocols intentionally and preserve fallback behavior.
- Update docs, fixtures, and tests when struct or schema fields change.

## Pattern Matching

- Use pattern matching and guards to make supported cases visible.
- Keep ordering specific to general. Do not let broad map/list patterns swallow
  edge cases.
- For binaries and protocols, validate size, encoding, and version markers
  before destructuring untrusted data.
