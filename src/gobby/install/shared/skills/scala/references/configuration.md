# Configuration

## Establish The Build Contract

- Read `build.sbt`, `project/*.scala`, `project/*.sbt`, `build.sc`,
  `project.scala`, Maven/Gradle files, and repository wrappers that own the
  affected module.
- Determine the exact Scala version from `scalaVersion`,
  `crossScalaVersions`, Scala CLI directives, or the build tool. Scala 2.13,
  Scala 3, and shared cross-build source sets have different valid syntax.
- Match the Java toolchain and target bytecode. Check compiler plugins and
  library suffixes such as `_2.13` and `_3` before changing dependencies.
- Preserve resolver policy, version catalogs, dependency locking, eviction
  checks, generated sources, and reproducible build settings.

## Compiler And Source Settings

- Reuse the repository's `scalacOptions`. Confirm option spelling against the
  active compiler because Scala 2 and Scala 3 flags differ.
- Keep warning and feature gates enabled. Use migration or rewrite modes only as
  an intentional, reviewed migration step.
- Preserve `-source`, explicit-nulls, strict-equality, language imports, macro,
  SemanticDB, and tasty-reader settings when they define the source contract.
- Treat compiler plugins as code-generation or typing dependencies. Version them
  with the compiler matrix and validate generated output.

## Formatting And Analysis

- Use the checked-in Scalafmt version and dialect. A Scala 3 source set needs a
  Scala 3-capable dialect; shared builds may select a cross-compatible dialect.
- Run Scalafix with the repository's SemanticDB configuration and rule set.
  Review semantic rewrites as source changes.
- Treat Metals/BSP diagnostics as fast feedback. The build tool remains the
  authoritative compiler and test runner.

## Focused Commands

Prefer the project's wrapper. Common shapes include:

```bash
sbt "module/Test/compile"
sbt "module/testOnly com.acme.OrderSpec"
mill module.compile
mill module.test.testOnly com.acme.OrderSpec
scala-cli compile path/to/source
scala-cli test path/to/source --test-only com.acme.OrderSpec
```

Run cross-version (`+`) or cross-platform targets when the changed API or source
is shared across those matrices. Keep the first pass scoped to the affected
module so failures identify the relevant boundary.
