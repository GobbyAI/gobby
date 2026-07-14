---
name: scala
description: "Enforces default Scala coding standards for agents writing or refactoring Scala: dialect and build configuration, Scala 3 type design and contextual abstractions, effects and resources, JVM and multiplatform boundaries, focused testing, performance, and concurrency. Use before editing Scala unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: scala, scalac, sbt, mill, scala-cli, metals, bsp, scalafmt, scalafix, munit, scalatest, scalacheck, cats, cats-effect, zio, akka, pekko, play, spark
sources:
  - "Primary: official Scala 3 Reference, independently summarized for Gobby: https://docs.scala-lang.org/scala3/reference/"
  - "Primary: official Scala 3 Book guidance on context parameters and opaque types: https://docs.scala-lang.org/scala3/book/ca-context-parameters.html and https://docs.scala-lang.org/scala3/book/types-opaque-types.html"
  - "Primary: official sbt testing documentation: https://www.scala-sbt.org/1.x/docs/Testing.html"
  - "Seed provenance only: EtaCassiopeia/claude-skills scala3-best-practices was reviewed for topic discovery; no repository license declared at authoring time, so no text or code copied: https://github.com/EtaCassiopeia/claude-skills"
---

# Scala

Default coding standards for Scala. Repository conventions and configured
tooling take precedence. First identify the Scala dialect, cross-build matrix,
build tool, effect system, target platforms, and formatter settings.

## Tooling

Run the repository's configured formatter, linter, compile task, and focused
tests before finishing. Use existing wrappers and CI tasks when available:

- Format/lint: Scalafmt, Scalafix, WartRemover, Scapegoat, or repo wrappers
- Compile: focused sbt, Mill, Scala CLI, Maven, or Gradle target
- Tests: focused MUnit, ScalaTest, Weaver, ZIO Test, JUnit, or framework target
- IDE feedback: Metals/BSP diagnostics as a fast signal alongside build checks
- Packages: preserve lockfiles, dependency graphs, generated sources, and
  cross-published artifact settings

Keep compiler warnings, dialect settings, source compatibility, and static
analysis intact. Fix the underlying violation.

## Configuration

- Match the declared Scala 2/3 version and every `crossScalaVersions` target.
- Preserve build-tool, Java toolchain, compiler-plugin, SemanticDB, formatter,
  generated-source, and CI conventions.
- Scope commands to the affected module and configuration before broad builds.

For sbt, Mill, Scala CLI, Maven/Gradle, compiler options, cross-building,
Scalafmt, Scalafix, Metals, and BSP:
`get_skill_file(name="scala", path="references/configuration.md")`

## Types And Contextual Abstractions

- Use Scala 3 syntax when the source set is Scala 3-only. Preserve shared Scala
  2.13/3 sources and public compatibility where the build requires them.
- Choose opaque types, value classes, enums, sealed hierarchies, union types,
  and match types from their actual representation and API constraints.
- Keep `given` instances narrowly scoped and make public contextual contracts
  deliberate.
- Validate Java, serialization, configuration, database, and network data before
  constructing domain values.

For opaque types versus `AnyVal`, enums versus sealed hierarchies,
`given`/`using`, extensions, equality, variance, and public types:
`get_skill_file(name="scala", path="references/types-and-contextual-abstractions.md")`

## Effects Errors And Resources

- Follow the repository's established model: direct style, `Future`, Cats
  Effect, ZIO, Akka/Pekko, or another explicit stack.
- Represent recoverable failures in the chosen error channel and preserve defect,
  cancellation, interruption, and retry semantics.
- Acquire resources with structured lifetime management. Keep blocking work off
  compute and event-loop executors.

For `Either`, `Try`, `Future`, effects, cancellation, blocking, retries, and
resource scopes:
`get_skill_file(name="scala", path="references/effects-errors-and-resources.md")`

## Framework And Platform Boundaries

- Isolate Java nullability and collection conversions, framework transport
  models, persistence schemas, and generated code from domain logic.
- Preserve Scala.js, Scala Native, JVM, Spark, Play, Akka/Pekko, Cats, and ZIO
  runtime constraints where present.
- Keep serialization formats, binary/source compatibility, and cross-platform
  source-set behavior explicit.

For Java interop, Scala.js/Native, web, actor, effect, data, and generated-code
boundaries:
`get_skill_file(name="scala", path="references/framework-and-platform-boundaries.md")`

## Testing

- Add focused tests for changed behavior, invalid inputs, effect failures,
  cancellation, resources, serialization, and platform boundaries.
- Add compile-time or negative compilation checks when a type-level guarantee is
  the behavior under test.
- Use deterministic schedulers and test runtimes provided by the project's stack.

For focused sbt/Mill/Scala CLI commands, MUnit, ScalaTest, Weaver, ZIO Test,
ScalaCheck, and compile-time checks:
`get_skill_file(name="scala", path="references/testing.md")`

## Performance And Concurrency

- Measure hot paths before optimizing collections, allocation, boxing, effects,
  streams, or concurrency.
- Keep execution contexts, fiber scopes, blocking pools, backpressure, and shared
  state ownership explicit.
- Confirm improvements with representative benchmarks or profiles and regression
  tests.

For JVM allocation, collections, specialization, JMH, async runtimes, fibers,
actors, and shared state:
`get_skill_file(name="scala", path="references/performance-and-concurrency.md")`

## Before You Finish

If you touched Scala: verify formatting/static analysis, focused compilation,
focused tests, and any relevant cross-version or platform target.
