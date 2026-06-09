# PHP Type And API Contracts

Use this reference when modeling domain data, request/response payloads,
collections, nullability, arrays, enums, PHPDoc, or package APIs.

## Strict Types

- Use `declare(strict_types=1);` for new strict code when consistent with the
  repo.
- Add native parameter, property, and return types wherever PHP supports them.
- Avoid dynamic properties and magic accessors for domain state unless the
  framework layer owns them.
- Make nullable values explicit with `?Type` and guard them before use.

## Domain Modeling

- Prefer value objects, readonly classes, enums, and DTOs for domain concepts.
- Avoid leaking raw `array<string, mixed>`, `mixed`, `stdClass`, request objects,
  or ORM entities into core services.
- Use named constructors or factories for validation-heavy types.
- Keep serialization DTOs separate from domain models when wire shape and
  invariants differ.

## PHPDoc And Static Analysis

- Use PHPDoc generics, array shapes, literal-string, non-empty-string, class-string,
  and template annotations when PHPStan/Psalm understands them.
- Keep PHPDoc in sync with native signatures.
- Prefer precise collection types such as `list<Account>`,
  `array<string, Account>`, or framework collection generics.
- Do not suppress `mixed` or nullability warnings without documenting the
  external boundary.

## Arrays And Collections

- Use arrays for simple local structures; introduce DTOs or collections when
  shape crosses function, module, framework, or package boundaries.
- Preserve list vs map semantics.
- Return immutable or copy-on-write data where callers should not mutate state.
- Validate decoded JSON and form data before treating it as a typed array shape.

## Public APIs

- Document package-facing APIs with parameter, return, exception, mutability, and
  security expectations.
- Keep backward compatibility in mind for packages, framework extensions,
  migrations, serialized payloads, and event contracts.
