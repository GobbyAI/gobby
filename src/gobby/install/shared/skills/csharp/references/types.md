# C# Types And API Contracts

Use the type system to make invalid states hard to represent.

## Nullable Reference Types

- Treat nullable annotations as part of the public contract.
- Avoid `!` except when a framework initializes values after construction and
  the invariant is documented.
- Prefer required members, constructors, options validation, or factory methods
  over nullable properties that are later assumed to exist.
- Validate external payloads before mapping to non-nullable domain types.

## Domain Modeling

- Use records for value-like request/response/domain objects when immutability
  and value equality are useful.
- Use small value objects for identifiers, money, durations, email addresses,
  slugs, and other constrained primitives.
- Use explicit result/error types when a method has expected failure modes.
- Avoid boolean flag parameters that create multiple behaviors; use separate
  methods, enums, or strategy objects.

## Generics And Variance

- Constrain generic parameters when callers need capabilities such as
  `notnull`, `struct`, `unmanaged`, or interface members.
- Prefer `IReadOnlyList<T>`, `IReadOnlyDictionary<TKey, TValue>`, and
  `IEnumerable<T>` at boundaries when callers should not mutate data.
- Materialize collections once when multiple enumeration would change behavior
  or repeat expensive work.

## Public API Compatibility

- Preserve source and binary compatibility for public packages unless the task
  explicitly allows breaking changes.
- Do not rename public types, change optional parameter defaults, remove
  overloads, or tighten nullability without checking callers.
- Keep exceptions, result shapes, JSON field names, and validation errors stable
  when external clients depend on them.

## Weak Typing To Avoid

- `dynamic`, `object`, `Dictionary<string, object>`, tuple-heavy APIs, and magic
  strings should stay at adapter boundaries.
- Convert weak input to typed DTOs, value objects, or domain models quickly.
- Keep reflection-based behavior behind small, tested adapters.
