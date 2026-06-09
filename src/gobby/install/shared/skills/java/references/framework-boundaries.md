# Java Framework Boundaries

Use this reference when changing Spring, Jakarta, Micronaut, Quarkus,
serialization, persistence, dependency injection, transactions, or framework
configuration.

## Boundary Shape

- Keep domain logic in plain Java when possible.
- Let controllers, listeners, repositories, schedulers, and configuration
  classes translate between framework types and domain contracts.
- Avoid leaking request, persistence, or framework annotations into core domain
  types unless the repo intentionally uses active-record or framework-domain
  coupling.
- Keep dependency injection explicit through constructors.

## Spring And Jakarta

- Prefer constructor injection over field injection.
- Keep bean scopes, transaction boundaries, validation annotations, and
  conditional configuration explicit.
- Avoid broad component scans that change application wiring outside the touched
  module.
- For configuration properties, validate required fields and defaults.

## Serialization

- Treat JSON/XML/message payloads as untrusted external input.
- Keep DTOs separate from domain models when wire compatibility differs from
  internal invariants.
- Test serialization round trips for public API payloads and persistence/event
  schemas.
- Be careful with Jackson annotations on records, sealed hierarchies, date/time
  types, and polymorphic payloads.

## Persistence

- Keep transaction boundaries close to use cases, not hidden deep in helpers.
- Avoid lazy-loading surprises in serializers, logs, equals/hashCode, and tests.
- Keep migrations, entity mappings, and repository queries in sync.
- Test dialect-specific behavior with the same database family when SQL or ORM
  behavior is the change.

## HTTP And Messaging

- Validate status codes, headers, content types, timeouts, retries, and
  idempotency.
- Translate client/framework exceptions into domain or service-layer failures at
  the adapter edge.
- Keep retry/backoff/circuit-breaker policy explicit and testable.
