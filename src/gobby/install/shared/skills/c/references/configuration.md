# C Configuration

Use the repo's build system as the source of truth. Do not introduce a second
configuration path unless the project already supports it.

## Build Systems

- Make: follow existing variable names, pattern rules, dependency generation,
  object directories, install targets, and phony targets.
- CMake: prefer target-scoped `target_sources`, `target_include_directories`,
  `target_compile_options`, `target_link_libraries`, and `target_compile_features`
  over global directory state.
- Meson: keep options typed, use dependency objects, and keep generated config
  headers explicit.
- Autotools: preserve `configure.ac`, `Makefile.am`, generated headers, and
  feature probes; do not hand-edit generated files unless the repo does.

## Compiler And Linker Flags

- Keep language standard flags intentional: `-std=c11`, `-std=c17`, `-std=c23`,
  or the project's configured equivalent.
- Keep warnings strict for new code. Prefer fixing diagnostics to suppressing
  them.
- Apply flags to the narrow target that needs them. Global flag changes can
  silently affect unrelated binaries.
- Keep debug, release, sanitizer, coverage, LTO, and cross-compilation modes
  separate.

## Includes And Generated Files

- Put public headers under the established include root and private headers near
  their implementation.
- Keep generated config headers and feature macros in one obvious place.
- Avoid relying on include-order accidents. Add direct includes for used types,
  macros, and functions.
- Preserve pkg-config, install, export, and downstream include-path behavior for
  libraries.

## Dependencies

- Reuse existing libc, platform, and project helper APIs before adding external
  libraries.
- When adding a dependency, update lockfiles, pkg-config checks, fallback logic,
  vendored metadata, and CI build scripts together.
- Treat optional dependencies as feature-gated API surface, not hidden runtime
  assumptions.

## Static Analysis

- Run configured `clang-tidy`, `cppcheck`, compiler warning targets, or wrappers
  on touched files.
- Keep suppressions local and justified with a comment when the warning is a
  deliberate platform or ABI tradeoff.
- Do not silence analyzer findings by weakening types, casts, or ownership
  checks.
