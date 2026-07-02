# Kotlin External Callee Provenance Spike

## Decision

Kotlin parser support should extract symbols, imports, and call sites now, but external callee classification should remain unresolved until Gradle and Maven dependency provenance is modeled.

Kotlin imports alone do not prove callable ownership. Static imports, top-level functions, extension functions, companion objects, overloads, and implicit default imports all require semantic or build-tool context before gcode can safely emit external call targets.

## Safe Current Boundary

- Capture `.kt` and `.kts` functions, classes, objects, imports, and direct call sites.
- Resolve same-file local calls where the existing parser can prove a unique callable.
- Store import edges for graph context.
- Leave dependency-backed external calls unresolved.

## Provenance Needed

- Gradle Kotlin/Groovy DSL dependency extraction.
- Maven `pom.xml` dependency extraction.
- Source-set and module ownership for multi-module builds.
- Import-to-package provenance for top-level functions and companion/static members.
- Collision checks against project-local packages and declarations.

## Fail-Closed Cases

- Extension function calls.
- Calls through implicit default imports.
- Unaliased package imports from external dependencies.
- Overloaded calls without semantic resolution.
- Companion object and Java static interop calls without proven target ownership.

_Last verified: 2026-05-24_
