---
name: java
description: "Enforces default Java coding standards for agents writing or refactoring Java: build configuration, API contracts, null-safety, resource handling, testing, concurrency, framework boundaries, and performance. Use before editing Java unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: java, javac, gradle, maven, pom.xml, build.gradle, build.gradle.kts, settings.gradle, junit, mockito, spring, jakarta
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Java build tools, JVM APIs, and framework boundaries."
  - "Secondary: Java project conventions around build reproducibility, Effective Java-style API design, JUnit testing, concurrency, and Spring/Jakarta boundaries."
---

# Java

Apply repository JDK, build, module, analyzer, framework, and generated-code rules first.

## Tooling

- Use checked-in Maven or Gradle wrappers, configured formatting and analysis,
  targeted compilation, and focused JUnit or TestNG tests.

## Configuration

- Preserve toolchains, dependency locks, modules, annotation processors, generated
  sources, formatter rules, and package boundaries.
- Diagnostic hook: treat javac, Error Prone, and NullAway findings as contract
  evidence; avoid raw types and `@SuppressWarnings` before fixing the modeled cause.

For build, dependency, module, and analysis setup:
`get_skill_file(name="java", path="references/configuration.md")`

## Type And API Contracts

- Represent domain states with records, sealed hierarchies, enums, value objects,
  or validated builders.
- Make nullability explicit and preserve public source, binary, and serialization contracts.

For nullability, generics, records, and API design:
`get_skill_file(name="java", path="references/types.md")`

## Error Handling

- Preserve causes when translating library, HTTP, database, file, or framework failures.
- Use try-with-resources and make checked, unchecked, or typed-result choices part
  of the public contract.

For exceptions and resources:
`get_skill_file(name="java", path="references/error-handling.md")`

## Testing

- Use JUnit 5, repository assertions, temporary resources, fake clocks, and
  framework integration tests at the boundary being changed.
- Keep persistence, HTTP, and serialization coverage real when those contracts matter.

For JUnit, Mockito, Testcontainers, and commands:
`get_skill_file(name="java", path="references/testing.md")`

## Concurrency

- Define ownership, thread safety, interruption, cancellation, and lifecycle before
  choosing executors, virtual threads, futures, locks, or reactive streams.
- Bound fan-out and blocking resource use.

For Java concurrency:
`get_skill_file(name="java", path="references/concurrency.md")`

## Framework Boundaries

- Keep controllers, dependency injection, persistence, messaging, and serializers
  as adapters around explicit domain behavior.
- Preserve transaction, validation, authorization, and lifecycle semantics.

For Spring, Jakarta, ORM, and serialization:
`get_skill_file(name="java", path="references/framework-boundaries.md")`

## Performance

- Inspect allocation, GC, query shape, contention, startup, and serialization evidence
  before changing collections, caching, pooling, or concurrency.

For JVM diagnostics and benchmarking:
`get_skill_file(name="java", path="references/performance.md")`
