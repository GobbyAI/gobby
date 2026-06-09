---
name: java
description: "Enforces default Java coding standards for agents writing or refactoring Java: build configuration, API contracts, null-safety, resource handling, testing, concurrency, framework boundaries, and performance. Use before editing Java unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: java, javac, gradle, maven, pom.xml, build.gradle, build.gradle.kts, settings.gradle, junit, mockito, spring, jakarta
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Java build tools, JVM APIs, and framework boundaries."
  - "Secondary: Java project conventions around build reproducibility, Effective Java-style API design, JUnit testing, concurrency, and Spring/Jakarta boundaries."
---

# Java

Default coding standards for Java. Repo conventions and configured tooling take
precedence. If `pom.xml`, `build.gradle`, `build.gradle.kts`, formatter config,
static-analysis rules, framework rules, or project instructions are stricter,
follow the repo.

## Tooling

Run the repo's configured format, lint/static analysis, compile, and targeted
tests before finishing. If none are configured, use:

- Format: the repo formatter, commonly Spotless, google-java-format, or IDE
  formatter config already checked in
- Static analysis: configured Checkstyle, PMD, SpotBugs, Error Prone, NullAway,
  ArchUnit, or framework-specific checks
- Compile: targeted Maven or Gradle compile tasks before claiming runtime safety
- Tests: focused JUnit/TestNG tests for changed packages, then broaden only when
  dependencies justify it
- Packages: use the checked-in Maven wrapper or Gradle wrapper when present

Do not bump Java versions, change dependency scopes, add annotation processors,
or relax compiler/static-analysis rules without a written reason tied to the
change.

## Configuration

- Match the repo's JDK, Maven/Gradle wrapper, toolchain, and dependency lock
  strategy.
- Keep generated sources, annotation processors, and formatter rules
  deterministic.
- Use package boundaries that reflect domain capabilities, not generic layers.
- Prefer standard library APIs before adding dependencies for collections, I/O,
  HTTP, JSON, time, concurrency, and logging.

For build, dependency, module, and static-analysis setup:
`get_skill_file(name="java", path="references/configuration.md")`

## Type And API Contracts

- Model domain states with records, sealed hierarchies, enums, value objects, or
  immutable classes instead of raw strings, `Map<String, Object>`, or nullable
  bags.
- Treat external data, deserialization, environment, and framework injection as
  untrusted boundaries.
- Make nullability explicit through repo-approved annotations, `Optional` return
  values where idiomatic, validation, or constructor invariants.
- Keep public APIs small, stable, and documented where they cross package or
  module boundaries.

For null-safety, generics, records, sealed types, and API design:
`get_skill_file(name="java", path="references/types.md")`

## Error Handling

- Use exceptions for exceptional failures and typed result/domain errors for
  expected business outcomes.
- Preserve causes when translating library, HTTP, database, file, or framework
  exceptions.
- Use try-with-resources for closeable resources and make cleanup behavior
  explicit.
- Validate inputs before mutation, persistence, or side effects.

For exception taxonomy, resource cleanup, and boundary translation:
`get_skill_file(name="java", path="references/error-handling.md")`

## Testing

- Add focused tests for changed behavior and failure paths, not only happy paths.
- Use JUnit 5 parameterized tests, assertions, temporary directories, fake clocks,
  and narrow mocks where they clarify observable behavior.
- Prefer integration tests with real serialization, persistence, HTTP, or Spring
  wiring when the behavior depends on those boundaries.
- Keep broad Gradle/Maven invocations for final confidence, not as the first or
  only proof.

For JUnit, Mockito, Testcontainers, Spring tests, and command selection:
`get_skill_file(name="java", path="references/testing.md")`

## Concurrency

- Make ownership, thread-safety, cancellation, and lifecycle explicit before
  using threads, executors, virtual threads, `CompletableFuture`, schedulers, or
  reactive APIs.
- Bound fan-out and resource use; do not create unbounded executors or blocking
  calls inside reactive/event-loop contexts.
- Propagate interruption and cancellation unless the repo has a stronger local
  pattern.

For executors, virtual threads, futures, locks, and reactive boundaries:
`get_skill_file(name="java", path="references/concurrency.md")`

## Framework Boundaries

- Keep Spring/Jakarta annotations, controllers, repositories, serializers, and
  dependency injection at the edge when domain code can stay plain Java.
- Validate framework-bound configuration, request payloads, and persistence
  models before they reach core logic.
- Avoid hiding dependencies behind static singletons, service locators, or
  framework globals.

For Spring/Jakarta, serialization, persistence, and DI boundaries:
`get_skill_file(name="java", path="references/framework-boundaries.md")`

## Performance

- Profile before optimizing and use JMH or production-like measurements for hot
  paths.
- Avoid speculative stream rewrites, excessive allocation, accidental quadratic
  collection work, and broad synchronization.
- Tune JVM, GC, caching, pooling, and database behavior only with evidence and
  rollback clarity.

For profiling, allocation, collections, and JVM performance:
`get_skill_file(name="java", path="references/performance.md")`

## Before You Finish

If you touched Java: verify formatting/static analysis where configured,
targeted compile, focused tests, and broader build tasks when changed modules or
framework wiring require them.
