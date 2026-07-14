# Types And Contextual Abstractions

## Model Domain Values

- Use a case class when the value has product structure and normal runtime
  identity. Validate invariants through a constructor or parsing boundary.
- Use an opaque type for a Scala 3-only, module-controlled abstraction over an
  existing representation when runtime wrapping is unnecessary. Expose smart
  constructors and only the operations consumers need.
- Preserve an existing `AnyVal` value class when Scala 2 cross-building, Java
  interop, public binary compatibility, or established representation makes it
  the correct contract. Check boxing behavior instead of assuming either form is
  free at every generic or interface boundary.
- Avoid raw `String`, numeric, or UUID values in public domain APIs when distinct
  meanings or validation rules would otherwise be conflated.

## Choose Closed Type Shapes

- Use an enum for a compact closed set of cases, including cases with parameters,
  when enum identity and generated APIs fit the contract.
- Use a sealed trait or class when the hierarchy needs an established public
  parent, shared constructor state, specialized inheritance, Java-facing shape,
  or compatibility with existing cases.
- Keep pattern matches exhaustive. Preserve an intentional fallback when inputs
  come from an open or versioned external domain.

## Contextual Abstractions

- Prefer `given` and `using` in Scala 3-only source. Keep `implicit` definitions
  and parameter clauses in Scala 2 or shared cross-build source where required.
- Define a given for a canonical capability or typeclass instance. Pass ordinary
  data explicitly when multiple values are routine or call-site clarity matters.
- Scope givens near their owning type or feature. Use explicit given imports and
  avoid wildcard contextual imports that make resolution hard to audit.
- Name public givens when their generated names could affect stable APIs.
- Use extension methods for focused operations owned by the abstraction. Keep
  imports and visibility narrow enough to prevent ambiguous enrichment.

## Public Type Contracts

- Write explicit result types for public methods, recursive definitions, given
  instances, and effect boundaries. Local implementation values may rely on
  inference when the inferred type is clear and stable.
- Preserve variance, higher-kinded parameters, path-dependent types, and type
  member bounds intentionally. Compile both producers and consumers after public
  variance changes.
- Use union, intersection, match, literal, and dependent types when they make an
  enforceable API guarantee. Prefer a named domain type when it communicates the
  contract more directly.
- Match the repository's equality policy. Under strict equality, derive or
  provide `CanEqual` only for comparisons the domain permits.
- Use `Option` for expected absence. Treat Java platform nulls and explicit-null
  unions as boundary data that must be normalized before entering domain logic.
