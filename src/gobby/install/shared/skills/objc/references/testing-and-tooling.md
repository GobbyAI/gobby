# Testing And Tooling

## Select Focused Commands

- Identify the owning workspace/project, scheme, configuration, destination,
  target, and test plan before running `xcodebuild`.
- Prefer the repository's wrapper. Otherwise use the smallest target build and
  `-only-testing:<TestBundle>/<TestCase>` or an equivalent test-plan filter.
- Run mixed-language compile tests when public headers, selectors, nullability,
  generics, blocks, errors, or module exposure change.
- Use the actual build system for compilation. A raw `clang` command commonly
  omits SDK paths, modules, prefix headers, generated headers, and per-file flags.

## Static Checks

- Run clang-format in check mode or the repository's formatter on changed source.
- Keep Clang warnings and `-Werror` policy at repository strength.
- Run the Clang Static Analyzer or `xcodebuild analyze` for changed ownership,
  Core Foundation, nil, and resource paths when configured.
- Review Xcode build logs for the effective `-fobjc-arc`/`-fno-objc-arc`, language
  mode, SDK, deployment, module, and warning flags.

## Behavioral Coverage

- Test success, nil, malformed input, recoverable error, cancellation, callback
  cardinality, callback queue, and teardown affected by the change.
- Exercise ARC/MRC ownership, copied blocks, weak relationships, autorelease pool
  boundaries, Core Foundation transfers, and non-object cleanup where relevant.
- Compile or run a Swift caller for Swift-facing headers and an Objective-C++ or C
  caller for shared C-family headers.
- Assert public error domains/codes and underlying errors instead of matching only
  localized descriptions.

## Dynamic Diagnostics

- Use Address Sanitizer for memory corruption, Thread Sanitizer for changed shared
  state, and Undefined Behavior Sanitizer where the target supports it.
- Use Instruments Allocations/Leaks, Zombies, or the repository's lifetime probes
  when ownership behavior changes.
- Keep sanitizer and Instruments runs focused and reproducible. Record the scheme,
  destination, input, and expected lifecycle.

## Completion Evidence

- Record exact build, analyzer, test, and runtime-diagnostic commands.
- State which target, memory mode, language mode, and client boundary each command
  covers.
- Report any unavailable simulator/device/SDK validation as a specific remaining
  gap; preserve a local compile or contract test where possible.
