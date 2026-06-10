# Type System And API Contracts

Use this reference when editing public APIs, domain models, nullability,
serialization shapes, Java interop, or multiplatform contracts.

## Null-Safety And State

- Prefer non-null types with validation at boundaries. Nullable types should mean
  a real domain state, not "we did not check yet."
- Model alternatives with sealed interfaces/classes, enums, value classes, data
  classes, and typed result objects instead of `Map<String, Any?>`, raw strings,
  or sentinel values.
- Avoid `!!` outside tiny, proven invariants. Prefer `requireNotNull`,
  validation, early returns, or typed wrappers with clear error messages.
- Keep collection mutability intentional. Expose read-only interfaces unless
  mutation is part of the contract.

## API Compatibility

- Preserve public function names, parameter order, default arguments, overloads,
  `suspend` modifiers, inline/value-class behavior, visibility, annotations, and
  binary compatibility unless the task explicitly changes the contract.
- Be careful with default parameters and generated overloads used from Java,
  reflection, frameworks, serialization, or dependency injection.
- Do not leak framework, persistence, transport, or platform models into domain
  APIs unless that is already the local boundary.

## Java And Platform Interop

- Treat Java platform types as nullable until proven otherwise. Normalize them at
  the edge before domain code sees them.
- Preserve `@JvmName`, `@JvmStatic`, `@JvmOverloads`, `@Throws`, nullability
  annotations, and SAM/adaptor behavior used by Java callers.
- Keep Android, Native, JavaScript, and JNI platform values behind wrappers that
  express lifecycle, threading, and availability constraints.

## Serialization And Persistence

- Validate external JSON, database, preference, bundle, intent, and message
  payloads before constructing domain models.
- Keep wire names, default values, polymorphic discriminators, migrations, and
  backwards compatibility explicit.
- Prefer small mapping functions over passing transport DTOs through the whole
  application.

## Review Checklist

- Are invalid states impossible or rejected at the boundary?
- Do Java and framework callers still see the same contract?
- Are `suspend`, `Flow`, nullable, and generic types part of the API story?
- Does the test suite cover both accepted and rejected states?
