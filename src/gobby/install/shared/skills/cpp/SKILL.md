---
name: cpp
description: "Enforces default C++ coding standards for agents writing or refactoring C++: build configuration, public interfaces, templates, ownership, error/resource boundaries, concurrency, testing, portability, and measured performance. Use before editing C++ unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: cpp, c++, cxx, hpp, cmake, conan, vcpkg, clang-tidy, sanitizer, templates
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for C++ build systems, public headers, template contracts, RAII ownership, exception safety, sanitizers, portability, and native test workflows."
  - "Secondary: common modern C++ project conventions around compiler warnings, static analysis, ABI stability, resource ownership, concurrency, test fixtures, and cross-platform build hygiene."
---

# C++

Apply repository compiler, ABI, standard-library, platform, and generated-code rules first.

## Tooling

- Use the configured formatter, compiler warnings, static analysis, focused build,
  tests, and existing sanitizer or Valgrind targets.
- Scope CMake, Meson, Bazel, Make, Conan, or vcpkg commands to the affected target.

## Configuration

- Preserve compiler, linker, standard-library, module, feature, preset, and package
  decisions already encoded by the build.
- Diagnostic hook: resolve warnings and `clang-tidy` findings at the named lifetime,
  conversion, or interface; require a documented invariant before suppression.

For build systems, flags, and analysis:
`get_skill_file(name="cpp", path="references/configuration.md")`

## Interfaces, Types, Templates, And ABI

- Treat public headers, modules, exported templates, inline definitions, layout,
  symbols, and calling conventions as compatibility contracts.
- Prefer value types, concepts, and spans or views whose lifetime is explicit.

For interface and ABI design:
`get_skill_file(name="cpp", path="references/types-templates-and-abi.md")`

## Ownership And Lifetime

- Prefer stack values, RAII, and `std::unique_ptr`; use shared ownership only when
  the object graph genuinely requires it.
- Check view lifetimes, iterator invalidation, bounds, narrowing, and move state.

For ownership and undefined-behavior traps:
`get_skill_file(name="cpp", path="references/ownership-and-lifetime.md")`

## Errors And Resources

- Follow the local exception, `std::expected`, status, or error-code convention.
- Preserve the strong or basic exception guarantee promised by the API and bind
  every file, lock, thread, transaction, and native handle to scoped cleanup.

For exception safety and resource cleanup:
`get_skill_file(name="cpp", path="references/errors-and-resources.md")`

## Concurrency

- Make synchronization, cancellation, thread affinity, and task ownership explicit.
- Prefer scoped locks and bounded executors; verify races with repository tooling.

For threads, atomics, coroutines, and race checks:
`get_skill_file(name="cpp", path="references/concurrency.md")`

## Testing

- Cover changed templates, ownership transitions, exception paths, concurrency,
  serialization, and ABI fixtures with the repository's native harness.
- Select sanitizers or fuzzers that can observe the changed boundary.

For harness and fixture selection:
`get_skill_file(name="cpp", path="references/testing.md")`

## Portability And Performance

- Guard platform APIs, compiler extensions, standard-library differences, alignment,
  byte order, and width assumptions.
- Keep measured hot-path changes compatible with supported compilers and targets.

For portability and profiling:
`get_skill_file(name="cpp", path="references/portability-and-performance.md")`
