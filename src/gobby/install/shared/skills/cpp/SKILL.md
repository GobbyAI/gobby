---
name: cpp
description: "Enforces default C++ coding standards for agents writing or refactoring C++: build configuration, public interfaces, templates, ownership, error/resource boundaries, concurrency, testing, portability, and measured performance. Use before editing C++ unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: cpp, c++, cxx, hpp, cmake, conan, vcpkg, clang-tidy, sanitizer, templates
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C++ build systems, public headers, template contracts, RAII ownership, exception safety, sanitizers, portability, and native test workflows."
  - "Secondary: common modern C++ project conventions around compiler warnings, static analysis, ABI stability, resource ownership, concurrency, test fixtures, and cross-platform build hygiene."
---

# C++

Default coding standards for C++. Repo conventions and configured tooling take
precedence. If compiler flags, build files, style docs, ABI notes, generated
headers, platform support rules, or project instructions are stricter, follow
the repo.

## Tooling

Run the repo's configured format, static analysis, build, sanitizer, and focused
test commands before finishing. If none are configured, use the local build
system:

- Format: repo wrapper, `clang-format`, or existing style tool scoped to touched files
- Analyze: configured `clang-tidy`, `include-what-you-use`, compiler warnings, or CI wrapper
- Build: touched CMake, Meson, Bazel, Make, Conan, vcpkg, or package target
- Tests: focused native test target, fixture binary, or project-specific script
- Sanitizers: ASan, UBSan, TSan, MSan, or Valgrind where the repo already uses them

Do not quiet warnings, disable sanitizers, lower language standards, or hide
undefined behavior to make a change pass.

## Configuration

- Match existing compiler, linker, standard-library, include-path, module,
  generated-header, sanitizer, warning, and cross-compilation conventions before
  adding files.
- Keep CMake presets, Meson options, Conan/vcpkg manifests, Bazel targets,
  exported compile commands, and toolchain files intentional.
- Prefer standard C++ and existing project helpers before adding dependencies or
  build-system layers.

For CMake, Meson, Bazel, Conan, vcpkg, compiler flags, and static analysis:
`get_skill_file(name="cpp", path="references/configuration.md")`

## Interfaces, Types, Templates, And ABI

- Treat public headers, modules, exported templates, and package interfaces as
  contracts. Keep ownership, lifetime, nullability, thread safety, exception,
  and ABI semantics explicit.
- Prefer value types, RAII wrappers, spans/views with clear lifetimes, concepts,
  and constrained templates over unbounded generic code.
- Keep exported layout, symbol visibility, calling conventions, inline
  functions, and template instantiations stable unless the change is an
  intentional ABI/API break.

For interface design, templates, modules, type choices, and ABI stability:
`get_skill_file(name="cpp", path="references/types-templates-and-abi.md")`

## Ownership And Lifetime

- Define one owner for every allocation, buffer, handle, file descriptor, lock,
  and borrowed view.
- Prefer stack values, RAII, `std::unique_ptr`, `std::shared_ptr` only for shared
  ownership, and project resource wrappers over raw owning pointers.
- Check bounds, iterator invalidation, object lifetimes, integer conversions,
  moved-from state, and exception safety before writing memory or resources.

For RAII, smart pointers, views, containers, cleanup, and undefined-behavior
traps: `get_skill_file(name="cpp", path="references/ownership-and-lifetime.md")`

## Errors And Resources

- Follow local conventions for exceptions, `std::expected`/result types,
  `std::error_code`, status objects, assertions, and process-boundary failures.
- Preserve platform error context before code that may clobber it.
- Release files, sockets, locks, transactions, threads, GPU handles, and other
  resources on every path.

For exception safety, status returns, cleanup paths, resources, and logging:
`get_skill_file(name="cpp", path="references/errors-and-resources.md")`

## Concurrency

- Make ownership, synchronization, cancellation, and thread-affinity explicit
  before introducing threads, task queues, coroutines, atomics, or callbacks.
- Prefer scoped locks, RAII thread/task handles, bounded executors, and immutable
  data sharing over detached work or hidden globals.
- Test races and cancellation with the tools already used by the repo.

For threads, locks, atomics, coroutines, async boundaries, and race testing:
`get_skill_file(name="cpp", path="references/concurrency.md")`

## Testing

- Add focused tests for changed behavior, public contracts, templates,
  boundary/failure paths, fixtures, allocator/resource cleanup, and
  ABI-visible behavior.
- Use the repo's test stack: GoogleTest, Catch2, doctest, Boost.Test, CTest,
  Meson test, Bazel test, custom fixtures, shell harnesses, or fuzz targets.
- Prefer small deterministic fixtures before broad integration binaries.

For native test harnesses, sanitizer/fuzzer selection, fixtures, and commands:
`get_skill_file(name="cpp", path="references/testing.md")`

## Portability And Performance

- Guard platform-specific APIs, compiler extensions, standard-library
  assumptions, alignment, endian behavior, and object-layout assumptions.
- Measure hot paths before optimizing. Check allocations, copies, parser loops,
  cache locality, syscalls, locking, virtual dispatch, and template/code-size
  costs.
- Keep readable, correct code first; use intrinsics, branch hints, custom
  allocators, polymorphic memory resources, or packed layouts only when the
  project has evidence and tests.

For portability, compiler/platform differences, profiling, and hot-path work:
`get_skill_file(name="cpp", path="references/portability-and-performance.md")`

## Before You Finish

If you touched C++: verify format/static analysis, a focused build, relevant
tests, and any configured sanitizer/fuzzer target pass before closing your work.
