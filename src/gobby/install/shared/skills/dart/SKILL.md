---
name: dart
description: "Enforces default Dart and Flutter coding standards for agents writing or refactoring Dart: pub configuration, null-safety, async boundaries, tests, Flutter state/UI boundaries, code generation, serialization, performance, and platform integration. Use before editing Dart unless the repo provides stricter local rules."
version: "1.0.0"
category: development
triggers: dart, flutter, pubspec.yaml, analysis_options.yaml, build_runner, build.yaml, freezed, json_serializable, flutter_test
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Dart, pub, analyzer rules, Flutter, and generated code workflows."
  - "Secondary: Dart project conventions around sound null-safety, async APIs, package layout, Flutter architecture, widget testing, and platform boundaries."
---

# Dart

Default coding standards for Dart and Flutter. Repo conventions and configured
tooling take precedence. If `pubspec.yaml`, `analysis_options.yaml`, `build.yaml`,
Flutter rules, lints, generated-code policy, or project instructions are
stricter, follow the repo.

## Tooling

Run the repo's configured format, analyzer, code generation, and focused tests
before finishing. If none are configured, use:

- Format: `dart format` for touched Dart files or the repo wrapper that invokes it
- Static analysis: `dart analyze` or `flutter analyze` for touched packages
- Tests: targeted `dart test`, `flutter test`, or package-specific test commands
- Codegen: the repo's configured `build_runner`, Freezed, JSON, Drift, Retrofit,
  or localization generation command when generated files are part of the change
- Packages: use `dart pub` or `flutter pub` with the checked-in lockfile policy

Do not bump SDK constraints, loosen analyzer lints, change dependency ranges, or
regenerate large generated surfaces without a reason tied to the change.

## Configuration

- Match the repo's Dart/Flutter SDK constraints, workspace or Melos layout,
  package manager policy, and dependency overrides.
- Keep `analysis_options.yaml`, generated files, localization config, and build
  targets deterministic.
- Preserve package boundaries; avoid importing across `lib/src` or feature
  layers in ways the package does not already expose.
- Prefer platform and Flutter SDK APIs before adding dependencies for state,
  serialization, HTTP, storage, routing, or dependency injection.

For pub, analyzer, workspace, generated-code, and package setup:
`get_skill_file(name="dart", path="references/configuration.md")`

## Type And API Contracts

- Treat sound null-safety as part of the API contract; avoid `dynamic`, forced
  non-null assertions, and nullable fields unless the boundary requires them.
- Model domain states with sealed classes, records, enums, value objects,
  extension types, or Freezed-style unions instead of untyped maps and flags.
- Keep public package APIs stable and expose behavior through intentional
  imports, not deep `lib/src` paths.
- Validate external JSON, platform channels, storage, and route arguments before
  they reach domain code.

For null-safety, sealed states, generics, package APIs, and DTO boundaries:
`get_skill_file(name="dart", path="references/types.md")`

## Async And Error Handling

- Make `Future`, `Stream`, cancellation, subscription ownership, and isolate
  work explicit.
- Translate network, platform, storage, parsing, and permission failures at the
  boundary where the dependency is known.
- Avoid unawaited futures, swallowed stream errors, widget lifecycle leaks, and
  UI updates after disposal.
- Preserve stack traces when mapping exceptions to domain failures.

For futures, streams, zones, cancellation, and failure translation:
`get_skill_file(name="dart", path="references/async-and-errors.md")`

## Testing

- Add focused tests for changed behavior, failure paths, serialization,
  generated-code contracts, widget behavior, and platform boundaries.
- Use unit tests for pure Dart logic and widget/integration tests when behavior
  depends on rendering, navigation, localization, providers, or plugins.
- Keep golden tests deterministic by controlling fonts, device size, locale,
  time, animation, and image/network inputs.

For Dart tests, Flutter tests, golden tests, fakes, and command selection:
`get_skill_file(name="dart", path="references/testing.md")`

## Flutter Boundaries

- Keep widgets declarative and move side effects, networking, persistence, and
  platform calls into repositories, services, controllers, blocs, notifiers, or
  the repo's established state boundary.
- Avoid storing derived domain state in widgets when it belongs in a model or
  state object.
- Respect `BuildContext` lifetime, mounted checks, navigation ownership,
  theming, localization, and accessibility semantics.

For widgets, state management, navigation, lifecycle, and platform/plugin edges:
`get_skill_file(name="dart", path="references/flutter-boundaries.md")`

## Serialization And Code Generation

- Keep generated files reproducible and commit them only when the repo does.
- Validate wire formats and migrations instead of assuming JSON or storage shape
  matches model constructors.
- Keep serializers, adapters, route generation, and API clients at boundaries.

For JSON, Freezed, build_runner, Drift, Retrofit, GraphQL, and generated files:
`get_skill_file(name="dart", path="references/serialization.md")`

## Performance And Platform

- Profile before optimizing with DevTools, timeline traces, frame rendering,
  memory snapshots, or focused benchmarks.
- Avoid rebuild storms, synchronous I/O on the UI isolate, unbounded streams,
  accidental large list work, image memory spikes, and plugin lifecycle leaks.
- Treat platform channels, permissions, background work, storage, and network
  state as fallible boundaries.

For Flutter rendering, isolates, memory, platform channels, and app lifecycle:
`get_skill_file(name="dart", path="references/performance.md")`

## Before You Finish

If you touched Dart: verify formatting, analyzer/lints, targeted tests, and any
configured code generation or Flutter validation relevant to the changed files.
