# Go Types And API Contracts Reference

Use Go's type system to make valid states easy to construct and invalid states hard to pass around.

## Domain Types

Prefer named types for important primitives.

```go
type UserID string

func ParseUserID(raw string) (UserID, error) {
    if raw == "" {
        return "", ErrInvalidUserID
    }
    return UserID(raw), nil
}
```

This keeps validation at the boundary and prevents unreviewed raw strings from becoming trusted IDs.

## Structs

- Use structs for cohesive data with stable fields.
- Keep exported fields intentional and documented.
- Avoid map-shaped data past parsing or adapter boundaries.
- Prefer zero-value usability when it makes the type safer and simpler.

## Interfaces

Accept interfaces where behavior varies. Return concrete types unless an interface is the public contract.

Small interfaces near the consumer keep dependencies testable without forcing every implementation to know about a broad abstraction.

## Generics

Use generics for shared containers, algorithms, or API helpers where callers get compile-time safety. Avoid generic indirection that hides simple domain logic or makes error messages harder to understand.

## External Data

Validate JSON, form data, CLI args, files, environment variables, queue messages, and network responses before constructing domain types. Treat `map[string]any` and `any` as boundary-only tools.

## Public APIs

Exported identifiers need doc comments and stable behavior. Keep constructors explicit when a type has invariants, dependencies, or validation.
