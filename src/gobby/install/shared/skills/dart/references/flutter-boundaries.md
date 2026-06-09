# Flutter Boundaries

Use this reference when editing widgets, state management, navigation, theming,
localization, accessibility, or platform/plugin integration.

## Widgets

- Keep `build` declarative and side-effect free.
- Move network, persistence, permissions, analytics, platform calls, and business
  rules behind services, repositories, controllers, blocs, notifiers, or the
  repo's established boundary.
- Prefer small widgets with clear inputs over widgets that reach into global
  state or service locators from many places.
- Keep keys, semantics, focus, and accessibility behavior intentional.

## State

- Store source-of-truth state in the repo's state layer, not duplicated in
  widgets.
- Model loading, empty, data, and error states with typed states rather than
  scattered nullable fields.
- Avoid rebuild storms by keeping selectors/listeners scoped and stable.
- Dispose owned resources and unsubscribe in lifecycle methods.

## Navigation And Context

- Keep route arguments typed and validated.
- Avoid using `BuildContext` after async gaps unless guarded by `mounted` or the
  local navigation pattern.
- Keep dialogs, snackbars, and navigation effects owned by the UI boundary, not
  domain services.

## Platform And Plugins

- Wrap plugin calls in a boundary that can be faked in tests.
- Treat permissions, lifecycle, app backgrounding, connectivity, file access, and
  platform channel payloads as fallible.
