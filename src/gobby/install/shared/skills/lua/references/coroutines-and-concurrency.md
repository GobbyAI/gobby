# Coroutines And Concurrency

## Coroutine Protocol

- Treat a coroutine as a stateful protocol with explicit owner, inputs, yielded
  outputs, terminal results, errors, and cleanup.
- `coroutine.create` creates a suspended coroutine. `coroutine.resume` returns a
  leading boolean followed by yielded/returned values or an error object.
- Branch on the leading status before consuming the remaining results. Preserve
  the error object's traceback context when diagnostics require it.
- Define what each yield means. Do not use positional values as an undocumented
  control protocol.
- Prefer `create`/`resume` where the caller needs explicit status handling.
  `coroutine.wrap` changes the error surface by raising through the wrapper.

## Cancellation And Cleanup

- Define who may abandon or cancel a suspended coroutine and how its resources
  are released.
- On Lua 5.4, use `coroutine.close` when appropriate and handle its status/error
  result. Verify compatibility before relying on it.
- Keep cleanup correct when a coroutine returns, raises, is closed, or remains
  suspended during host shutdown.
- Test yields inside nested Lua calls and failures after one or more successful
  yields.

## Host Yield Boundaries

- A coroutine yield cannot cross every C call frame. Use the continuation APIs
  (`lua_yieldk`, `lua_callk`, `lua_pcallk`) when a C function must support a
  yieldable operation.
- Keep continuation context valid for the entire suspended lifetime. Do not
  retain pointers or stack assumptions that the API does not preserve.
- Follow framework-owned scheduling rules in OpenResty, game engines, plugins,
  and other embedded hosts. A plain Lua coroutine is not a substitute for the
  framework's event loop contract.

## Concurrency

- Lua coroutines are cooperative and execute on the calling OS thread. Add an
  explicit scheduler for fairness, blocking I/O, timeouts, and backpressure.
- Never perform blocking host work on an event-loop thread unless the framework
  explicitly permits it.
- Confine each Lua state to the host's synchronization model. Coordinate shared
  native data outside Lua with explicit ownership or locks.
