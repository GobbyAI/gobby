---
name: dart
description: "Enforces default Dart and Flutter coding standards for agents writing or refactoring Dart: pub configuration, null-safety, async boundaries, tests, Flutter state/UI boundaries, code generation, serialization, performance, and platform integration. Use before editing Dart unless the repo provides stricter local rules."
version: "1.1.0"
category: development
triggers: dart, flutter, pubspec.yaml, analysis_options.yaml, build_runner, build.yaml, freezed, json_serializable, flutter_test
sources:
  - "Primary: Gobby TypeScript language skill reference pattern, adapted for Dart, pub, analyzer rules, Flutter, and generated code workflows."
  - "Secondary: Dart project conventions around sound null-safety, async APIs, package layout, Flutter architecture, widget testing, and platform boundaries."
---

# Dart

Apply repository SDK, analyzer, package, Flutter, and generated-code rules first.

## Tooling

- Use configured `dart format`, `dart analyze` or `flutter analyze`, targeted
  tests, and repository code-generation commands.
- Preserve lockfile policy and generated-file ownership.

## Configuration

- Match SDK constraints, workspace or Melos layout, lint includes, flavors,
  localization, assets, and build targets.
- Diagnostic hook: resolve analyzer findings at the nullability or ownership
  boundary; avoid `dynamic`, forced unwraps, broad casts, and ignores as escape hatches.

For pub, analyzer, workspace, and codegen setup:
`get_skill_file(name="dart", path="references/configuration.md")`

## Type And API Contracts

- Use sound null safety, sealed states, records, enums, and value objects to
  represent domain variants.
- Validate JSON, routes, storage, and platform-channel values before domain use.

For type and package API patterns:
`get_skill_file(name="dart", path="references/types.md")`

## Error Handling

- Translate network, storage, permission, parsing, and platform failures at the
  adapter that owns them, preserving stack traces.

## Concurrency

- Make `Future`, `Stream`, subscription ownership, cancellation, and isolate
  boundaries explicit.
- Avoid unawaited work and close subscriptions with the lifecycle that created them.

For futures, streams, isolates, and failures:
`get_skill_file(name="dart", path="references/async-and-errors.md")`

## Testing

- Use unit coverage for Dart logic and widget or integration coverage for rendered,
  navigation, plugin, lifecycle, or platform behavior.
- Control fonts, device size, locale, time, and animations in golden tests.

For Dart, Flutter, and golden test selection:
`get_skill_file(name="dart", path="references/testing.md")`

## Flutter Boundaries

- Keep widgets declarative; put networking, storage, and side effects behind owned
  state or service boundaries.
- Respect `BuildContext`, mounted state, navigation ownership, and plugin lifecycle.

For UI, state, and platform edges:
`get_skill_file(name="dart", path="references/flutter-boundaries.md")`

## Serialization And Code Generation

- Regenerate from source and preserve repository policy for committed outputs.
- Treat generated adapters and clients as wire-format boundaries.

For Freezed, JSON, Drift, Retrofit, and build_runner:
`get_skill_file(name="dart", path="references/serialization.md")`

## Performance And Platform

- Inspect frame timing, rebuild scope, UI-isolate blocking, memory, streams, and
  platform-channel traffic before changing architecture.

For Flutter runtime and platform analysis:
`get_skill_file(name="dart", path="references/performance.md")`
