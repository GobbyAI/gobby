# C++ Errors And Resources

Use this reference when editing error paths, cleanup code, process boundaries,
or resource-owning APIs.

## Error Conventions

- Follow local conventions first: exceptions, `std::expected` or project result
  types, `std::error_code`, status objects, bool-plus-output, or assertions.
- Use exceptions for exceptional failures only when the surrounding code uses
  them. Keep domain failures explicit at API boundaries.
- Add context when translating errors across layers; preserve the original
  platform or library error where useful.

## Exception Boundaries

- Do not let exceptions cross C ABI, plugin, thread, coroutine, callback,
  destructor, or FFI boundaries unless the boundary explicitly supports it.
- Catch at process, request, task, thread, or callback boundaries where logging,
  cleanup, and error translation can happen once.
- Avoid throwing from destructors and cleanup callbacks.

## Resource Cleanup

- Own files, sockets, locks, transactions, processes, memory mappings, GPU
  resources, and native handles with RAII wrappers whenever possible.
- Make close, flush, commit, rollback, cancellation, timeout, and partial-write
  behavior explicit.
- Join or stop threads and task handles on every path.

## errno And Platform Errors

- Preserve `errno`, `GetLastError`, or library-specific error state before
  calling code that may clobber it.
- Convert platform errors at the boundary where callers can act on them.

## Logging

- Log failures at boundaries and include enough context to diagnose the failing
  operation.
- Do not log secrets, raw tokens, large buffers, or personally sensitive data.
- Keep assertions for programmer invariants; return or throw for runtime
  failures callers can encounter.
