---
name: kotlin
description: "Enforces default Kotlin coding standards for agents writing or refactoring Kotlin: Gradle and compiler configuration, null-safe API contracts, coroutine error handling, Android/JVM/KMP boundaries, testing, performance, and concurrency. Use before editing Kotlin unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: kotlin, kotlinc, gradle, kts, android, kmp, multiplatform, coroutines, flow, junit, mockk, detekt, ktlint
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Kotlin, Gradle, coroutines, Android, JVM, and multiplatform workflows."
  - "Secondary: common Kotlin project conventions around null-safety, structured concurrency, Kotlin/JVM interop, Android lifecycle boundaries, KMP source sets, testing, and static analysis."
---

# Kotlin

Apply repository compiler, Gradle, Android/JVM/KMP, analysis, and generated-code rules first.

## Tooling

- Use checked-in Gradle wrappers and configured format, lint, Detekt, compile,
  focused test, and platform targets.

## Configuration

- Preserve Kotlin/JVM and plugin versions, compiler options, source sets, dependency
  catalogs, KSP/KAPT outputs, Android variants, and KMP targets.
- Diagnostic hook: treat compiler and Detekt findings as nullability or ownership
  evidence; avoid `!!`, unchecked casts, `@Suppress`, and widened platform types.

For Gradle, compiler, source-set, and generated-code setup:
`get_skill_file(name="kotlin", path="references/configuration.md")`

## Type System And API Contracts

- Use nullable types, sealed hierarchies, data/value classes, and explicit results
  to model domain states.
- Normalize Java platform types and serialized data at narrow boundaries.

For Kotlin types and API contracts:
`get_skill_file(name="kotlin", path="references/type-system-and-api-contracts.md")`

## Error Handling

- Translate transport, persistence, parsing, authorization, and platform failures
  at their owning adapter while preserving causes.
- Keep cancellation distinct from domain failure.

## Concurrency

- Use structured coroutine scopes, explicit dispatchers, bounded fan-out, and owned
  `Flow` collection.
- Propagate cancellation and tie jobs to Android, server, or KMP lifecycles.

For coroutines and failure mapping:
`get_skill_file(name="kotlin", path="references/coroutines-and-error-handling.md")`

## Framework And Platform Boundaries

- Keep Android components, Compose, DI, persistence, serialization, Java interop,
  and expect/actual code at explicit adapters.
- Preserve lifecycle, threading, source-set, and platform capability contracts.

For Android, JVM, and KMP boundaries:
`get_skill_file(name="kotlin", path="references/framework-and-platform-boundaries.md")`

## Testing

- Use repository JUnit, Kotest, MockK, coroutine-test, Android, Compose, or KMP
  harnesses at the boundary being changed.
- Control dispatchers, virtual time, lifecycle, and platform fixtures.

For test selection and commands:
`get_skill_file(name="kotlin", path="references/testing.md")`

## Performance

- Inspect allocation, boxing, coroutine scheduling, Flow buffering, Compose
  recomposition, startup, and platform interop for affected workloads.

For runtime and concurrency analysis:
`get_skill_file(name="kotlin", path="references/performance-and-concurrency.md")`
