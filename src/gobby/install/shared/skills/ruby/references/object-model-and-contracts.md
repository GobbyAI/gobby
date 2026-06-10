# Ruby Object Model And Contracts

## Public API Shape

- Treat public methods, keyword arguments, blocks, callbacks, constants, mixins,
  inheritance hooks, and module inclusion order as API contracts.
- Preserve arity and keyword compatibility. Ruby 2.7+ keyword behavior can make
  wrapper changes source-incompatible.
- Use explicit return shapes for domain behavior: value object, result object,
  enum-like object, tuple convention, or documented exception.
- Keep metaprogramming local, named, and tested. Avoid hidden method generation
  when a normal method communicates the behavior.

## Classes, Modules, And Mixins

- Prefer cohesive classes and modules with one responsibility over dumping
  behavior into concerns.
- Keep ActiveSupport::Concern callbacks, class methods, and included blocks
  minimal and predictable.
- Avoid monkey patches unless the repo already isolates them and tests version
  compatibility.
- Do not hide business rules inside callbacks, validators, scopes, or global
  configuration.

## Types And Signatures

- If the repo uses Sorbet, Steep, or RBS, update signatures with code changes.
- Use typed structs/value objects where they clarify domain state.
- Avoid `T.untyped`, `T.unsafe`, broad `Object`, or loose RBS types unless the
  boundary is truly dynamic; narrow at the edge.
- Keep nilability explicit. Do not use nil, false, or empty collections as
  interchangeable sentinels.

## Ruby Idioms

- Use keyword arguments for named options and domain parameters.
- Use blocks for scoped resource behavior, not hidden control flow.
- Prefer pattern matching or clear guard clauses only when they improve the
  model.
- Keep frozen constants, immutable values, and memoization thread-aware.

## Compatibility Checklist

- Callers still see the same method names, arity, keywords, blocks, exceptions,
  and return shapes.
- Serialization, Active Model naming, validations, callbacks, routes, jobs, and
  mailers still use expected names.
- Public gem APIs, CLI flags, config keys, and extension points remain stable or
  have migration coverage.
