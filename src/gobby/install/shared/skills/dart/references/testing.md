# Dart Testing

Use this reference for selecting focused tests and building reliable Dart or
Flutter coverage.

## Test Shape

- Unit-test pure Dart logic with `test`.
- Use widget tests when behavior depends on rendering, layout, navigation,
  providers, localization, inherited widgets, gestures, or semantics.
- Use integration tests only when platform plugins, app lifecycle, database,
  network stack, or device behavior is the behavior under test.
- Add failure-path tests for parsing, validation, permissions, repository errors,
  and state transitions.

## Flutter Reliability

- Control device size, text scale, locale, theme, fake time, and animations when
  they affect assertions.
- Avoid real network, timers, storage, and platform plugins in unit/widget tests;
  use fakes or test bindings.
- For golden tests, keep fonts, image inputs, animation frames, and pixel ratio
  deterministic.

## Generated And Serialized Contracts

- Test serializers, adapters, generated routes, generated clients, and storage
  migrations when the change affects their contract.
- Regenerate owned files before running tests that depend on generated output.
- Do not update snapshots or goldens without inspecting the behavioral change.

## Commands

Prefer repo scripts. Common focused commands:

```sh
dart test test/path_test.dart
dart analyze lib/path test/path_test.dart
flutter test test/widget_test.dart
flutter test integration_test/app_test.dart
```
