---
name: c
description: "Enforces default C coding standards for agents writing or refactoring C: compiler/build configuration, headers and ABI contracts, ownership, resource cleanup, error paths, tests, portability, and measured performance. Use before editing C unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: c, c11, c17, c23, header, make, cmake, meson, autotools, pkg-config, sanitizer
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C build systems, headers, ABI contracts, ownership, undefined behavior, sanitizers, portability, and native test workflows."
  - "Secondary: common C project conventions around compiler warnings, static analysis, allocator ownership, errno/result boundaries, fixture tests, and cross-platform build hygiene."
---

# C

Default coding standards for C. Repo conventions and configured tooling take
precedence. If compiler flags, build files, style docs, generated headers, ABI
notes, or platform support rules are stricter, follow the repo.

## Tooling

Run the repo's configured format, static analysis, build, sanitizer, and focused
test commands before finishing. If none are configured, use the local build
system:

- Format: repo wrapper, `clang-format`, or existing style tool scoped to touched files
- Analyze: configured `clang-tidy`, `cppcheck`, compiler warnings, or CI wrapper
- Build: touched Make, CMake, Meson, Autotools, or package target
- Tests: focused native test target, fixture binary, or project-specific script
- Sanitizers: ASan, UBSan, TSan, MSan, or Valgrind where the repo already uses them

Do not quiet warnings, disable sanitizers, lower language standards, or hide
undefined behavior to make a change pass.

## Configuration

- Match existing compiler, linker, include-path, generated-header, pkg-config,
  sanitizer, warning, and cross-compilation conventions before adding files.
- Keep build outputs, generated config headers, feature macros, and platform
  defines intentional.
- Prefer standard C and existing project helpers before adding new dependencies
  or build-system layers.

For Make, CMake, Meson, Autotools, compiler flags, and static analysis:
`get_skill_file(name="c", path="references/configuration.md")`

## Headers And ABI

- Treat headers as contracts. Keep ownership, lifetime, nullability, thread
  safety, feature macros, visibility, and error semantics explicit.
- Use fixed-width, size-aware, and opaque types where ABI or serialization
  stability matters.
- Keep public structs, enum values, symbol names, calling conventions, and
  exported layout stable unless the change is an intentional ABI break.

For headers, type choices, struct layout, visibility, and ABI stability:
`get_skill_file(name="c", path="references/types-and-abi.md")`

## Memory And Lifetime

- Define one owner for every allocation, buffer, handle, file descriptor, lock,
  and borrowed pointer.
- Check sizes, bounds, integer conversions, allocation failures, and string
  termination before writing memory.
- Prefer cleanup labels, scoped helper APIs, or project cleanup macros over
  duplicated partial-cleanup paths.

For ownership, cleanup, bounds, allocation, and undefined-behavior traps:
`get_skill_file(name="c", path="references/memory-and-lifetime.md")`

## Errors And Resources

- Preserve errno or platform error context before calling code that may clobber
  it.
- Return typed status, negative errno, bool-plus-output, or project result types
  consistently with the surrounding code.
- Close resources on every path and make retry, cancellation, timeout, and
  partial-write behavior explicit.

For error conventions, cleanup paths, file/socket/process resources, and logging:
`get_skill_file(name="c", path="references/errors-and-resources.md")`

## Testing

- Add focused tests for changed behavior, bounds, failure paths, parser fixtures,
  allocator failures, errno/resource cleanup, and ABI-visible contracts.
- Use the repo's test stack: Check, CMocka, Unity, Criterion, CTest, Meson test,
  custom fixtures, shell harnesses, or fuzz targets already present.
- Prefer small deterministic fixtures before broad integration binaries.

For native test harnesses, sanitizer/fuzzer selection, fixtures, and commands:
`get_skill_file(name="c", path="references/testing.md")`

## Portability And Performance

- Guard platform-specific APIs, feature-test macros, alignment assumptions,
  endian behavior, and compiler extensions.
- Measure hot paths before optimizing. Check allocations, copies, parsing loops,
  cache locality, syscalls, locking, and vectorization claims.
- Keep readable, correct code first; use intrinsics, branch hints, custom
  allocators, or packed layouts only when the project has evidence and tests.

For portability, compiler/platform differences, profiling, and hot-path work:
`get_skill_file(name="c", path="references/portability-and-performance.md")`

## Before You Finish

If you touched C: verify format/static analysis, a focused build, relevant tests,
and any configured sanitizer/fuzzer target pass before closing your work.
