# C Testing

Use tests to lock down behavior, memory safety, resource cleanup, and ABI-visible
contracts. Prefer the repo's harness over a new framework.

## Test Harness Selection

- Use existing Check, CMocka, Unity, Criterion, CTest, Meson, shell, or custom
  fixture harnesses.
- Add a narrow test binary or fixture only when no existing target can exercise
  the behavior.
- Keep test data small, deterministic, and checked into the same fixture pattern
  the repo already uses.
- Avoid broad `make test` or `ctest` runs as the only evidence when a focused
  target exists.

## What To Cover

- Success and failure paths for changed behavior.
- Boundary lengths, empty input, malformed input, non-ASCII or binary data where
  relevant, and integer overflow cases.
- Allocation failure, cleanup after partial initialization, double-close guards,
  and errno preservation.
- Public header compilation and ABI-visible struct or enum changes.
- Platform-specific branches when the change touches portability.

## Sanitizers And Dynamic Checks

- Use ASan and UBSan for memory, bounds, and undefined-behavior fixes when the
  project supports them.
- Use TSan for concurrency changes only when the code can run deterministically
  enough to make the signal useful.
- Use Valgrind or leak checkers where sanitizer builds are not available.
- Add fuzzer or corpus cases for parser and decoder fixes when the repo already
  has fuzz infrastructure.

## Test Doubles

- Prefer real small fixtures for parsers and serializers.
- Wrap file, clock, socket, random, and process APIs behind existing seams only
  when the production code already supports it.
- Keep fake allocators and failure injection narrow; broad global hooks can make
  tests order-dependent.

## Validation Commands

Run commands scoped to touched targets, for example:

- `cmake --build build --target account_tests`
- `ctest --test-dir build -R account`
- `meson test -C build account`
- `make check TESTS=account_test`
- repo wrapper plus configured sanitizer target

Record the exact focused command that proves the change.
