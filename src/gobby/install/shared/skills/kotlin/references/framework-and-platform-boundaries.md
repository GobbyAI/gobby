# Framework And Platform Boundaries

Use this reference when editing Android, Compose, KMP, Ktor, Spring, persistence,
serialization, dependency injection, or platform interop code.

## Android And Compose

- Keep Activity, Fragment, View, Compose, ViewModel, navigation, permission, and
  lifecycle code at the edge. Domain code should not depend on Android classes
  unless the local architecture already does.
- Preserve main-thread rules, configuration changes, saved state, process death,
  permissions, and lifecycle cancellation.
- In Compose, keep state hoisting, stability, recomposition cost, side effects,
  previews, and accessibility explicit.
- Do not put long-running or blocking work on the main thread.

## Multiplatform

- Keep common code portable. Platform APIs belong in `expect`/`actual` or
  platform source sets.
- Preserve source-set dependencies and target-specific behavior. A JVM-only
  dependency in `commonMain` is a build break for other targets.
- Test the platform source set affected by the change, not only JVM tests.

## Server And Framework Code

- Keep Ktor/Spring controllers, routes, dependency injection, persistence,
  serializers, and framework annotations at the boundary when domain logic can
  stay plain Kotlin.
- Validate request payloads, environment, config, database rows, and external
  responses before constructing domain types.
- Avoid static service locators and hidden framework globals. Prefer explicit
  constructor dependencies consistent with the repo.

## Persistence And Serialization

- Preserve schema migrations, transaction boundaries, idempotency, indexes, and
  data compatibility.
- Keep DTOs separate from domain models when wire or storage shape can change
  independently.
- Validate polymorphic serialization, default values, enum additions, and unknown
  field behavior.

## Interop

- Wrap Java, JNI, Objective-C/Swift, JavaScript, and platform APIs with typed
  adapters that make nullability, threading, lifecycle, and error behavior clear.
- Do not let platform-specific threading or lifecycle assumptions leak into
  common/domain code.
