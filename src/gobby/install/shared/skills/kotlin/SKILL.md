---
name: kotlin
description: "Enforces default Kotlin coding standards for agents writing or refactoring Kotlin: Gradle and compiler configuration, null-safe API contracts, coroutine error handling, Android/JVM/KMP boundaries, testing, performance, and concurrency. Use before editing Kotlin unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: kotlin, kotlinc, gradle, kts, android, kmp, multiplatform, coroutines, flow, junit, mockk, detekt, ktlint
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Kotlin, Gradle, coroutines, Android, JVM, and multiplatform workflows."
  - "Secondary: common Kotlin project conventions around null-safety, structured concurrency, Kotlin/JVM interop, Android lifecycle boundaries, KMP source sets, testing, and static analysis."
---

# Kotlin

Default coding standards for Kotlin. Repo conventions and configured tooling
take precedence. If Gradle, Maven, Android, KMP, Detekt, ktlint, compiler flags,
coroutine rules, or project instructions are stricter, follow the repo.

## Tooling

Run the repo's configured format, lint/static analysis, compile, and focused
tests before finishing. If none are configured, use the local Kotlin project:

- Format/lint: ktlint, Detekt, Spotless, Android lint, or repo wrappers
- Compile/type checks: targeted Gradle/Maven compile task for changed source sets
- Tests: focused JVM, Android, KMP, coroutine, or framework test target
- Packages: preserve Gradle wrapper, version catalog, lockfile, and toolchain
- Runtime checks: Android instrumentation, Compose previews, Ktor/Spring smoke, or
  KMP platform checks where the changed boundary depends on them

Do not relax compiler flags, nullability annotations, Detekt/ktlint rules,
dependency versions, coroutine dispatchers, or source-set wiring to make a quick
change pass.

## Configuration

- Match the repo's Kotlin version, Gradle/Maven wrapper, Android Gradle Plugin,
  Kotlin Multiplatform targets, Java toolchain, KSP/KAPT, serialization,
  Compose, dependency lock, and CI conventions.
- Keep compiler options, source sets, generated code, and dependency scopes
  deterministic.
- Prefer standard library, kotlinx, platform, and framework APIs already in use
  before adding dependencies.

For Gradle/Maven, compiler flags, Android/KMP, KSP/KAPT, Detekt, ktlint, and CI:
`get_skill_file(name="kotlin", path="references/configuration.md")`

## Type System And API Contracts

- Model states with non-null types, sealed hierarchies, value classes, data
  classes, enums, and explicit result types instead of nullable maps or strings.
- Treat Java interop, serialization, platform APIs, environment, and network
  data as untrusted boundaries.
- Preserve public API binary/source compatibility, variance, suspend contracts,
  inline/value-class behavior, and annotations unless the change is intentional.

For null-safety, platform types, data classes, sealed types, value classes, and
API compatibility:
`get_skill_file(name="kotlin", path="references/type-system-and-api-contracts.md")`

## Coroutines And Error Handling

- Use structured concurrency. Avoid `GlobalScope`, unbounded launch trees, hidden
  dispatcher switches, swallowed `CancellationException`, and blocking calls in
  suspend code.
- Preserve exception causes when translating library, HTTP, database, platform,
  or framework failures.
- Make Flow, Channel, callback, resource, timeout, retry, and cancellation
  behavior explicit and testable.

For suspend functions, Flow, cancellation, retry, result shapes, and cleanup:
`get_skill_file(name="kotlin", path="references/coroutines-and-error-handling.md")`

## Framework And Platform Boundaries

- Keep Android lifecycle, Compose state, ViewModel, DI, persistence, Ktor, Spring,
  serialization, and platform interop boundaries separate from domain code.
- Validate request, persistence, JNI, Java, JavaScript, Native, and Android
  platform data before it reaches core logic.
- Preserve KMP `commonMain`, `expect`/`actual`, platform source-set, and
  dependency boundaries.

For Android, Compose, KMP, Ktor, Spring, persistence, serialization, and DI:
`get_skill_file(name="kotlin", path="references/framework-and-platform-boundaries.md")`

## Testing

- Add focused tests for changed behavior, failure paths, coroutine timing,
  platform boundaries, serialization, and source-set behavior.
- Use the repo's stack: kotlin-test, JUnit, MockK, Turbine,
  kotlinx-coroutines-test, Robolectric, Android instrumentation, Ktor, Spring, or
  Testcontainers.
- Prefer deterministic unit and boundary tests before broad Gradle invocations.

For Kotlin/JVM, Android, coroutine, Flow, KMP, and framework test commands:
`get_skill_file(name="kotlin", path="references/testing.md")`

## Performance And Concurrency

- Measure hot paths before optimizing. Check allocation, boxing, collection
  shape, sequence/flow overhead, dispatcher contention, Android main-thread work,
  and native/JS target behavior.
- Keep ownership, immutability, thread confinement, dispatcher choice, and
  cancellation explicit.
- Use batching, backpressure, caching, value classes, lazy evaluation, or
  background work only with evidence and tests.

For allocation, collections, Flow, dispatchers, Android UI thread, and KMP
runtime behavior:
`get_skill_file(name="kotlin", path="references/performance-and-concurrency.md")`

## Before You Finish

If you touched Kotlin: verify formatting/lint, compile/static analysis, focused
tests, and any relevant Android, KMP, coroutine, or framework checks pass before
closing your work.
