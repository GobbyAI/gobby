# Scala External Callee Provenance Spike

## Decision

Scala external callee classification should remain unresolved until build-tool and semantic provenance are available.

Imports alone are insufficient because Scala supports implicit imports, extension methods, givens/implicits, overloaded `apply`, package objects, wildcard imports, and JVM interop. Safe classification needs dependency metadata plus compiler-grade symbol resolution for many call shapes.

## Provenance Needed

- SBT, Mill, Maven, and Gradle dependency extraction.
- Project package and source-set ownership.
- Import alias and selector parsing.
- SemanticDB or Metals/Bloop-backed symbol resolution for external definitions.
- Collision checks against local packages, package objects, and companion objects.

## Safe Future Boundary

Classify only symbol-resolved calls where SemanticDB or an equivalent semantic provider returns exactly one definition owned by an external dependency, with no local package collision.

## Fail-Closed Cases

- Wildcard imports.
- Extension methods and implicit/given-provided calls.
- Unqualified calls from implicit imports.
- Overloaded `apply` and constructor-like calls without semantic resolution.
- Java static interop without resolved owner metadata.

_Last verified: 2026-05-24_
