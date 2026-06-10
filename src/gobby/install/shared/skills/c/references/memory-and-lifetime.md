# C Memory And Lifetime

Make ownership visible in names, APIs, and cleanup paths. Most serious C bugs
come from ambiguous lifetime, unchecked sizes, or incomplete cleanup.

## Ownership

- Define which caller owns every returned pointer, borrowed pointer, handle,
  buffer, and string.
- Pair allocators and deallocators from the same subsystem.
- Keep ownership transfer explicit in function names or comments for public APIs.
- Do not store borrowed pointers beyond the lifetime documented by the caller.

## Allocation

- Check every allocation unless the project has a terminating allocator contract.
- Use overflow-safe allocation helpers for `count * size` calculations.
- Initialize memory before exposing it to callers or error paths.
- Free partially initialized objects through one cleanup function where possible.

## Buffers And Strings

- Track buffer length separately from pointer identity.
- Validate lengths before indexing, copying, formatting, parsing, or appending.
- Prefer bounded formatting and copying helpers already used by the repo.
- Preserve NUL termination contracts explicitly; binary buffers are not strings.

## Cleanup

- Use a single cleanup label or project cleanup macro for functions with multiple
  resources.
- Release resources in reverse acquisition order when order matters.
- Set output parameters only after success unless the API documents partial
  results.
- Avoid double-free and use-after-free by keeping one owner variable per
  resource.

## Undefined Behavior

- Check shifts, signed overflow, unaligned access, strict aliasing, lifetime,
  out-of-bounds access, and invalid pointer arithmetic.
- Do not rely on unspecified evaluation order.
- Keep volatile, atomics, and memory barriers limited to code that truly needs
  them.
- Prefer sanitizer-backed tests for fixes that touch memory or lifetime behavior.
