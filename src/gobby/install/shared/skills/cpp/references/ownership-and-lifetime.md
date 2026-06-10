# C++ Ownership And Lifetime

Use this reference when editing allocations, containers, handles, views,
iterators, cleanup paths, or code that can hit undefined behavior.

## Ownership

- Prefer stack values and RAII members. Move to dynamic allocation only when the
  lifetime or polymorphism requires it.
- Use `std::unique_ptr` for unique ownership and `std::shared_ptr` only when
  shared lifetime is truly part of the design.
- Do not store raw owning pointers. Raw pointers and references should be
  borrowed, non-owning views.
- Keep ownership transfer explicit with returns, `std::move`, factories, or
  project resource wrappers.

## Borrowed Views

- Check `std::span`, `std::string_view`, iterators, references, and pointer
  members for dangling lifetimes.
- Do not return views into temporaries or containers that may reallocate.
- Validate bounds and sizes before slicing, indexing, casting, copying, or
  parsing buffers.

## Containers And Iterators

- Understand invalidation rules before mutating containers while holding
  iterators, references, or views.
- Reserve capacity only when measurements or obvious input sizes justify it.
- Prefer standard algorithms when they make iterator ranges and mutations
  clearer.

## Exception Safety

- Maintain invariants across partial construction, moves, callbacks, and
  exceptions.
- Prefer commit/swap, RAII guards, and local temporaries for multi-step state
  updates.
- Mark functions `noexcept` only when they really cannot throw and the guarantee
  is useful.

## Undefined Behavior Traps

- Check integer conversions, signed overflow, alignment, object lifetime,
  strict aliasing, null dereferences, use-after-move, data races, and
  uninitialized reads.
- Use sanitizers and focused tests for risky memory or lifetime changes.
- Do not paper over UB with casts or warning suppressions.
