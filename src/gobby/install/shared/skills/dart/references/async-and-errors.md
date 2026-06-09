# Dart Async And Error Handling

Use this reference for `Future`, `Stream`, isolates, cancellation, widget
lifecycle, and boundary failure handling.

## Futures

- Await futures that affect state, output, persistence, or validation.
- Mark intentionally detached futures with the repo's established `unawaited`
  pattern and make error handling explicit.
- Preserve stack traces when mapping exceptions with `Error.throwWithStackTrace`
  or equivalent local helpers.
- Keep retries bounded and observable.

## Streams And Subscriptions

- Own stream subscriptions explicitly and cancel them in the right lifecycle.
- Handle stream errors; do not assume `listen` receives only data events.
- Prefer typed stream states over ad hoc nullable fields for loading/error/data.
- Avoid broadcasting streams unless multiple listeners are required and tested.

## Flutter Lifecycle

- Check `mounted` before updating UI after an async gap.
- Do not hold a `BuildContext` across async work unless the local pattern proves
  it is safe.
- Dispose controllers, focus nodes, animation controllers, text controllers,
  subscriptions, timers, and platform handles.
- Keep side effects out of `build`.

## Boundary Failures

- Translate HTTP, file, storage, isolate, platform-channel, permission, parsing,
  and plugin failures at the edge.
- Keep user-facing errors safe and logs useful without exposing secrets.
- Test failure states for critical user flows, not just successful async paths.
