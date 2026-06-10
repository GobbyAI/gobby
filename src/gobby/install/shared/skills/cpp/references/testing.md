# C++ Testing

Use this reference when adding or updating C++ tests, fixtures, fuzzers,
sanitizer runs, or validation commands.

## Test Stack Selection

- Use the repo's existing framework: GoogleTest, Catch2, doctest, Boost.Test,
  CTest, Meson test, Bazel test, custom binaries, shell harnesses, or fuzz
  targets.
- Keep tests close to the target they validate and use the build-system target
  that already owns that code.
- Do not add a new framework for a narrow change unless the repo has no
  reasonable local test path.

## Behavior Coverage

- Test public behavior, boundary conditions, parser fixtures, error paths,
  resource cleanup, cancellation, and ABI-visible contracts.
- Add template/type tests with compile-only tests, static assertions, or focused
  instantiation tests when public templates changed.
- Include negative/failure cases, not only happy paths.

## Sanitizers And Analysis

- Use ASan/UBSan for memory and undefined-behavior risk.
- Use TSan for concurrency changes when supported by the toolchain.
- Use MSan, Valgrind, fuzzers, or leak checkers when the repo already relies on
  them or the change touches risky input handling.

## Fixtures

- Prefer small deterministic fixtures with clear ownership and cleanup.
- Avoid broad integration tests for small library behavior when a focused unit
  or component target can prove the contract.
- Keep temporary files, sockets, environment variables, and threads isolated per
  test.

## Validation Commands

- Scope builds and tests to touched targets where possible.
- Use the repo wrapper or exact build invocation from CI when available.
- Record the exact command and result before finishing.
