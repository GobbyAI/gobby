# Errors And Resources

## Error Contracts

- Follow the surrounding API's convention: raise with `error`, return
  `nil, err`, return a leading status, or translate into the host's result type.
- Keep operational failures distinct from programmer-contract violations when
  the codebase makes that distinction.
- An error object can be any Lua value. Preserve it until a boundary requires a
  stable string or typed host representation.
- Check every protected-call status before reading success results. Keep the
  stack/result tuple aligned with that status.

## Protected Calls And Tracebacks

- Use `pcall` for a protected boundary. Use `xpcall` when a message handler must
  capture a traceback before stack unwinding removes useful frames.
- Do not catch an error only to discard it. Add operation context once and
  preserve the original cause or object according to repository conventions.
- In a C host, use `lua_pcall`/`lua_pcallk` around untrusted or recoverable chunk
  execution. Handle syntax, runtime, memory, and message-handler statuses as the
  API requires.
- Functions such as `lua_error` and `luaL_check*` do not return normally. Keep C
  resources out of unsafe spans or arrange cleanup under the host's configured
  Lua error mechanism.

## Resource Lifetimes

- Prefer explicit close/finally-style control or Lua 5.4 to-be-closed variables
  for deterministic release.
- Implement `__close` as a small, repeatable cleanup operation. Decide how a
  cleanup error combines with an existing error.
- Use `__gc` as a last-resort backstop. Collection timing is nondeterministic,
  finalizers cannot yield, and finalizer errors become warnings.
- Unreference C registry handles, close files/sockets, release userdata-owned
  memory, and detach host callbacks on every exit path.
- Make shutdown idempotent where both explicit close and a finalizer can reach
  the same resource.

## Boundary Translation

- Convert host exceptions/status codes into the module's documented Lua error
  shape. Convert Lua failures into stable host diagnostics without reading
  success values from a failed stack.
- Keep secrets and oversized payloads out of error messages and tracebacks.
