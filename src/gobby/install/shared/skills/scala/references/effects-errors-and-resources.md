# Effects Errors And Resources

## Preserve The Project's Effect Model

- Identify whether the module uses direct exceptions, `Either`/`Try`, `Future`,
  Cats Effect, ZIO, actors, or another runtime. Keep one coherent model through
  each call chain.
- Use an explicit typed error channel for recoverable domain and boundary
  failures when the selected stack supports it. Reserve defects and exceptions
  for broken invariants, programmer errors, or APIs whose contract requires
  throwing.
- Preserve causes and useful context when translating errors. Avoid catching
  broad throwables that include fatal JVM conditions, cancellation, or
  interruption.

## Asynchrony And Cancellation

- A `Future` is eager. Accept or summon the intended `ExecutionContext`, keep
  blocking calls off general compute pools, and preserve failed-future causes.
- In Cats Effect or ZIO, compose effects through the runtime's operators. Keep
  unsafe execution at application boundaries and preserve fiber interruption,
  finalizers, and structured scopes.
- In actor systems, keep mutable state actor-confined and make ask timeouts,
  supervision, restarts, and message failure semantics explicit.
- Propagate cancellation across callbacks, streams, Java futures, and effect
  bridges. Tests should cover cancellation during acquisition and use.

## Resources And Blocking

- Use `Using`, Cats Effect `Resource`, ZIO scoped resources, or the repository's
  established lifetime abstraction for files, sockets, clients, executors, and
  transactions.
- Acquire and release in the same structured scope. Release partially acquired
  resources and preserve the primary failure when cleanup also fails.
- Route blocking I/O to the runtime's blocking facility or a bounded dedicated
  executor. Document thread affinity for native, UI, database, and event-loop
  APIs.

## Retry And Recovery

- Retry only operations known to be transient and safe under the operation's
  idempotency contract.
- Bound retries by attempts or elapsed time, add backoff/jitter where appropriate,
  and keep the terminal cause observable.
- Distinguish fallback values from failure suppression. A fallback must satisfy
  a documented domain contract and have tests for the degraded path.
