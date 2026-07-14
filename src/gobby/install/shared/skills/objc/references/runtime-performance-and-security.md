# Runtime Performance And Security

## Runtime Features

- Use dynamic dispatch, selectors, forwarding, associated objects, and swizzling
  only where the runtime behavior is the intended extension point.
- Keep swizzling scoped to known classes/selectors, install it once, preserve the
  original implementation, and test load-order and subclass behavior.
- Choose associated-object ownership and synchronization to match the value's
  lifetime. Avoid attaching mutable global state to arbitrary objects.
- Validate dynamic class names, selectors, KVC keys, notification names, and
  plugin identifiers before resolving or invoking them.

## Serialization And Inputs

- Use secure-coding APIs and explicit allowed classes for archived external data.
  Treat decoded object graphs as untrusted after class filtering.
- Keep format strings constant when arguments contain external text. Preserve
  Clang format diagnostics for logging and variadic methods.
- Validate lengths, integer conversions, encodings, pointer/count pairs, and
  buffer ownership at C boundaries.
- Avoid logging tokens, credentials, personal data, raw request bodies, or
  sensitive `NSError.userInfo` content.

## Measure Before Optimizing

- Profile allocations, autorelease pool growth, retain/release traffic, collection
  copying, bridging, dynamic messaging, locks, queues, and I/O with representative
  workloads.
- Reduce ownership churn or copying only after confirming the lifetime and
  mutability contracts remain correct.
- Cache only with a bounded lifetime, invalidation rule, memory-pressure behavior,
  and thread-safety contract.
- Keep hot-path C or C++ conversions behind narrow typed adapters and measure the
  full bridge cost.

## Unsafe Boundaries

- Isolate `unsafe_unretained`, raw pointers, IMP calls, C callbacks, varargs, and
  manual byte manipulation behind small reviewed functions.
- Document the lifetime, alignment, type, thread, and cleanup preconditions next
  to the unsafe boundary.
- Add regression tests for invalid inputs and teardown, then use sanitizers or
  runtime diagnostics appropriate to the failure mode.
