# Dart Types And API Contracts

Use this reference for null-safety, public APIs, model design, and data flowing
across package, network, storage, platform, or UI boundaries.

## Sound Null-Safety

- Make nullable states explicit and narrow them before use.
- Avoid `!` except at boundaries with a proven invariant and nearby explanation.
- Avoid `dynamic` and `Object?` in domain logic; parse external data into typed
  DTOs or domain values before use.
- Use `required`, default values, constructors, assertions, or validation to keep
  impossible states out of the model.

## Domain Shape

- Use enums, sealed classes, records, value objects, or Freezed unions for states
  that should not be represented by strings, maps, or boolean flags.
- Keep IDs, money, dates, durations, locales, and permissions typed when they
  cross boundaries.
- Prefer immutable models for shared state unless mutation is clearly owned.
- Keep equality/hash behavior intentional for models used in state management,
  caching, collections, or widgets.

## Public APIs

- Keep package exports small and stable.
- Avoid leaking generated, framework, or transport-specific types into core
  domain APIs unless the repo has chosen that architecture.
- Make async contracts clear: a method that may wait should return `Future`, a
  stream should document lifecycle and close ownership through tests or naming.

## Boundary Validation

- Validate JSON shape, route arguments, platform channel payloads, storage rows,
  deep links, and environment/config values before constructing domain objects.
- Represent expected validation failures as typed failures or result states where
  the UI or caller needs to recover.
