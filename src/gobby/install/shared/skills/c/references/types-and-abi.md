# C Types And ABI

C headers are executable contracts. Treat every public type, macro, enum, and
symbol as something downstream code may compile against.

## Public Headers

- Document ownership, lifetime, nullability, thread-safety, and error behavior
  in public declarations when names alone are insufficient.
- Keep public headers self-contained. A downstream file should be able to include
  the header without relying on unrelated prior includes.
- Use include guards or the repo's established pragma style consistently.
- Hide private implementation details behind opaque structs or internal headers.

## Type Choices

- Use `size_t` for object sizes and indexes where negative values are invalid.
- Use fixed-width integer types for wire formats, file formats, ABI contracts,
  and serialization.
- Use enums for closed status or mode sets only when their underlying ABI and
  persistence behavior are acceptable.
- Avoid naked `int` for lengths, booleans, handles, and flags when a clearer
  project type exists.

## Struct Layout

- Do not reorder public struct fields casually. Field order, padding, alignment,
  and size may be ABI.
- Prefer opaque handles for public library state that may evolve.
- Keep packed structs limited to wire or hardware layouts and isolate unaligned
  access behind helper functions.
- Initialize structs through constructors, designated initializers, or project
  helpers that make default values explicit.

## Symbols And Visibility

- Keep exported names, visibility attributes, calling conventions, and version
  scripts aligned with the project.
- Prefix public symbols in C libraries to avoid downstream collisions.
- Keep `extern "C"` guards in headers used by C++ consumers.
- Preserve source and binary compatibility unless the task explicitly calls for
  a breaking change.

## Casts And Conversions

- Treat casts as boundary markers. Each cast should have a reason.
- Check narrowing conversions, signed/unsigned comparisons, pointer arithmetic,
  and `void *` recovery paths.
- Keep aliasing rules, alignment, and effective types intact. Avoid type punning
  through incompatible pointers.
