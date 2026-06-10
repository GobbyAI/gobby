# C++ Configuration

Use this reference when editing C++ build files, compiler options, package
manifests, generated files, or static-analysis settings.

## Build Systems

- CMake: keep targets explicit. Prefer target-local include directories,
  compile features, definitions, warnings, and link libraries over global flags.
- Meson: use typed options, dependency objects, and per-target settings. Keep
  generated config headers and feature probes deterministic.
- Bazel: keep `cc_library`, `cc_binary`, `cc_test`, toolchain, and visibility
  declarations narrow.
- Make or custom scripts: preserve existing variables, generated-file order,
  parallel-build safety, and dependency tracking.

## Standards And Toolchains

- Match the existing C++ standard unless the task intentionally upgrades it.
- Keep compiler-specific flags guarded by compiler and platform checks.
- Preserve ABI-relevant settings such as standard library, RTTI, exceptions,
  visibility, architecture, LTO, sanitizer, and debug-info flags.
- Do not mix incompatible runtime libraries or toolchains casually.

## Dependencies

- Follow the repo's package manager: Conan, vcpkg, system packages, vendored
  code, FetchContent, submodules, or Bazel external deps.
- Pin versions using the repo's existing policy. Do not introduce floating
  dependency refs for reproducible builds.
- Keep transitive dependency exposure intentional on public targets.

## Static Analysis

- Prefer existing `clang-tidy`, `include-what-you-use`, compiler warnings,
  formatting, and sanitizer wrappers.
- Add suppressions only when narrow, documented, and tied to an external
  boundary, generated code, or a known analyzer false positive.
- Keep compile databases current when analysis tools depend on them.

## Generated Code

- Identify whether generated headers, protobufs, flatbuffers, bindings, or
  source files are committed or ignored before changing them.
- Regenerate with the owning command instead of hand-editing generated output,
  unless the repo explicitly stores patched generated files.
- Keep generated output deterministic across machines.
