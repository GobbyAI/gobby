---
name: c
description: "Enforces default C coding standards for agents writing or refactoring C: compiler/build configuration, headers and ABI contracts, ownership, resource cleanup, error paths, tests, portability, and measured performance. Use before editing C unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: c, c11, c17, c23, header, make, cmake, meson, autotools, pkg-config, sanitizer
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C build systems, headers, ABI contracts, ownership, undefined behavior, sanitizers, portability, and native test workflows."
  - "Secondary: common C project conventions around compiler warnings, static analysis, allocator ownership, errno/result boundaries, fixture tests, and cross-platform build hygiene."
---

# C

Apply repository compiler, ABI, platform, and generated-code rules first.

## Tooling

- Use the configured formatter, compiler warnings, static analysis, focused build,
  tests, and existing sanitizer or Valgrind targets.
- Scope Make, CMake, Meson, or Autotools commands to the affected target when possible.

## Configuration

- Preserve compiler, linker, feature-macro, include-path, generated-header, and
  platform decisions already encoded by the build.
- Prefer standard C and existing helpers before adding a dependency or extension.
- Diagnostic hook: investigate each new warning at the operation it names; avoid
  casts or warning suppression until the ownership, conversion, or ABI claim is proven.

For build systems, flags, and analysis:
`get_skill_file(name="c", path="references/configuration.md")`

## Headers And ABI

- Treat headers as contracts for ownership, lifetime, nullability, thread safety,
  struct layout, symbols, calling conventions, and serialized values.
- Use opaque and fixed-width types where ABI or wire compatibility requires them.

For public types and ABI:
`get_skill_file(name="c", path="references/types-and-abi.md")`

## Memory And Lifetime

- Assign one owner to each allocation, buffer, handle, descriptor, lock, and mapping.
- Check sizes, bounds, conversions, allocation failures, and string termination at
  the boundary where the risky value enters.

For cleanup and undefined-behavior traps:
`get_skill_file(name="c", path="references/memory-and-lifetime.md")`

## Errors And Resources

- Preserve `errno` or platform error state before another call can overwrite it.
- Follow the local status convention and release every acquired resource on retry,
  cancellation, timeout, and partial initialization.

For result conventions and cleanup:
`get_skill_file(name="c", path="references/errors-and-resources.md")`

## Testing

- Exercise changed bounds, parser inputs, allocation failures, cleanup, and ABI
  fixtures with the repository's native harness.
- Select sanitizers or fuzzers that can observe the changed lifetime or input boundary.

For harness and fixture selection:
`get_skill_file(name="c", path="references/testing.md")`

## Portability And Performance

- Guard platform APIs, feature-test macros, alignment, byte order, and width assumptions.
- Keep measured hot-path changes compatible with supported compilers and architectures.

For portability and profiling:
`get_skill_file(name="c", path="references/portability-and-performance.md")`
