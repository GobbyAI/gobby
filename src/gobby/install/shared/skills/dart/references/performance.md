# Dart Performance And Platform

Use this reference for Flutter rendering, isolate work, memory, platform
channels, storage, and app lifecycle.

## Measure First

- Use Flutter DevTools, timeline traces, memory snapshots, frame charts, or
  focused benchmarks before optimizing hot paths.
- Keep performance assertions tied to observable behavior such as frame budget,
  memory growth, startup time, network calls, or query count.

## Flutter Rendering

- Avoid unnecessary rebuilds by scoping listeners/selectors and keeping widget
  inputs stable.
- Avoid synchronous I/O, large JSON parsing, image decoding, or CPU-heavy work on
  the UI isolate.
- Use lists, caching, image sizing, pagination, and keys deliberately.
- Keep animations bounded and dispose controllers.

## Async Work And Isolates

- Move CPU-heavy parsing or transforms off the UI isolate when measurement shows
  it matters.
- Bound streams, queues, retries, and parallel work.
- Keep cancellation and cleanup explicit for background tasks.

## Platform And App Lifecycle

- Treat platform channels, permissions, sensors, notifications, background work,
  connectivity, files, and secure storage as fallible and lifecycle-sensitive.
- Release native/plugin resources through the repo's platform boundary.
- Test app pause/resume, permission denial, and offline states when touched.
