# Java Type And API Contracts

Use this reference when modeling domain data, public APIs, serialization
boundaries, nullability, generics, records, sealed types, or collection
contracts.

## Domain Modeling

- Prefer records for immutable value carriers when invariants fit compact
  constructors.
- Prefer small immutable classes when validation, behavior, or controlled
  construction matters.
- Use enums for closed symbolic sets and sealed interfaces/classes for closed
  variant families with associated data.
- Avoid `Map<String, Object>`, raw `Object`, stringly typed IDs, and nullable DTO
  bags across service boundaries.
- Introduce value types for IDs, money, email addresses, tenant IDs, and other
  domain primitives when raw strings or numbers can be confused.

## Nullability

- Follow repo-approved nullability annotations if present.
- Validate constructor and factory inputs once, then keep fields non-null.
- Use `Optional` mainly for return values that may be absent; avoid `Optional`
  fields, parameters, and serialization payloads unless the repo already uses
  that convention.
- Prefer explicit empty collections over nullable collections.
- Do not silence nullness warnings with broad suppressions.

## Generics And Collections

- Avoid raw types. Preserve generic information through APIs.
- Use bounded wildcards for producer/consumer APIs when it makes call sites
  safer: `? extends T` for producers, `? super T` for consumers.
- Return unmodifiable views or immutable copies for public collections unless
  mutation is part of the contract.
- Pick collection types by semantics: ordered list, uniqueness set, keyed map,
  sorted set/map, queue, deque.

## API Boundaries

- Keep public methods explicit about ownership, nullability, mutability,
  threading, and exception behavior.
- Make serialization DTOs distinct from domain models when external shape and
  internal invariants differ.
- Keep framework annotations out of domain types when practical.
- Document public APIs that cross package, module, network, persistence, or
  plugin boundaries.

## Validation

- Validate external payloads, configuration, environment, and persistence data
  before constructing trusted domain objects.
- Prefer factories or compact constructors for invariant enforcement.
- Keep validation errors precise enough for callers and tests to identify the
  failing field or boundary.
