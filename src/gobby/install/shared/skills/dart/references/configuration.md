# Dart Configuration

Use this reference when editing Dart or Flutter package setup, analyzer config,
workspace files, generated-code settings, or dependency policy.

## SDK And Package Boundaries

- Match the `environment.sdk` and Flutter SDK constraints already in
  `pubspec.yaml`.
- Use the repo's wrapper, workspace, Melos, FVM, or package script when present.
- Keep public package imports intentional; avoid deep imports from another
  package's `lib/src` unless that is already the documented local convention.
- Do not add dependency overrides, path dependencies, git dependencies, or broad
  version ranges without a reason tied to the change.

## Analyzer And Lints

- Treat `analysis_options.yaml` as executable policy. Do not silence rules to
  make a change pass unless the repo already uses that suppression pattern.
- Prefer fixing null-safety, async, visibility, and lint findings at their source.
- Keep generated files excluded or included according to the existing analyzer
  policy.

## Generated Code

- Check `build.yaml`, annotations, builders, and output part files before editing
  generated models, routes, clients, serializers, or localization files.
- Do not hand-edit `.g.dart`, `.freezed.dart`, `.gr.dart`, generated localization,
  or generated API files unless the repo explicitly treats them as hand-owned.
- Run the narrow generation command that owns the changed files when generated
  output should change.

## Commands

Prefer the repo's scripts. Common fallbacks:

```sh
dart pub get
dart format path/to/file.dart
dart analyze
dart test test/path_test.dart
```

For Flutter packages:

```sh
flutter pub get
dart format lib test
flutter analyze
flutter test test/widget_test.dart
```
