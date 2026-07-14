# Configuration And Language Modes

## Inspect Before Editing

- Identify the owning Xcode target or non-Xcode build target, configuration, SDK,
  deployment target, architecture, and test target.
- Inspect target build settings and per-file compiler flags. `CLANG_ENABLE_OBJC_ARC`
  can be overridden by `-fobjc-arc` or `-fno-objc-arc` on individual files.
- Read `.clang-format`, warning flags, module settings, prefix headers, generated
  headers, bridging headers, umbrella headers, and CI commands already in use.
- Preserve target membership and file type metadata when adding or moving files.

## Select The Actual Language

- Compile `.m` files as Objective-C and `.mm` files as Objective-C++. Rename a
  file only when the implementation truly needs the other language mode and all
  consumers support the change.
- Treat `.h` and `.pch` files as context-dependent. A header included from C,
  Objective-C, Objective-C++, Swift import, or C++ must remain valid for each
  supported consumer.
- Guard Objective-C declarations with `__OBJC__` only when a shared C-family
  header genuinely needs separate surfaces. Avoid hiding accidental incompatibility.
- Keep C++ types out of Swift-imported or pure Objective-C public declarations;
  place them in `.mm` implementation details or an explicit facade.

## Match The Build Contract

- Preserve the repository's module strategy: textual imports, modules, framework
  umbrella headers, module maps, or precompiled headers.
- Match warning policy and static analyzer settings. Do not silence ownership,
  nullability, availability, selector, or format diagnostics to land a change.
- Preserve deployment availability annotations and weak-linking behavior. Compile
  against the minimum supported SDK/runtime combination where CI requires it.
- Keep generated sources and vendored code within their owning generation or
  update workflow.

## Validation

- Use `xcodebuild -showBuildSettings` or the build system's equivalent when the
  effective ARC, SDK, module, or deployment settings are unclear.
- Build the smallest owning target with its normal configuration and destination.
- Verify both Objective-C and Objective-C++ consumers when a shared header changes.
- Verify a Swift import when a public Apple-platform module header changes.
