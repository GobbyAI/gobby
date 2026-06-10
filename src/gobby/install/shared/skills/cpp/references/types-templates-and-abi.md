# C++ Types, Templates, And ABI

Use this reference when editing public headers, modules, templates, type models,
or ABI-visible code.

## Public Interfaces

- Treat headers, modules, exported templates, and installed package targets as
  contracts.
- Document ownership, lifetime, nullability, thread safety, exception behavior,
  and error semantics where callers need them.
- Keep includes minimal but sufficient. Do not rely on transitive includes in
  public headers.

## Type Modeling

- Prefer domain-specific value types over primitive soup for IDs, sizes, flags,
  and units.
- Use `enum class` for scoped variants and bitmask wrappers for flags.
- Use `std::optional`, `std::variant`, `std::span`, `std::string_view`, and
  project equivalents only when their lifetime and ownership semantics are
  clear at the call site.
- Avoid nullable raw pointers for ownership. If a pointer is nullable and
  borrowed, make that explicit in naming or documentation.

## Templates And Concepts

- Constrain templates with concepts, `requires`, traits, or static assertions
  when invalid instantiations would produce noisy errors.
- Keep generic code close to the abstraction it serves. Avoid template
  machinery for one-off call sites.
- Prefer explicit instantiations or narrow headers when template compile time or
  binary size becomes a real cost.
- Keep overload sets and forwarding references simple enough for callers to
  predict.

## ABI Stability

- Do not change exported class layout, virtual functions, enum values, symbol
  names, calling conventions, inline functions, or exception specifications
  without treating it as an ABI break.
- Hide implementation details behind PIMPL, opaque handles, private targets, or
  internal namespaces when ABI stability matters.
- Preserve symbol visibility and export macros on public APIs.

## Modules

- Keep module partitions and exports intentional. Do not export implementation
  helpers just to satisfy a local build error.
- Maintain compatibility with the repo's compiler support matrix before
  introducing modules or changing module boundaries.
