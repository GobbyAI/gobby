# Elixir OTP And Concurrency

## Process Ownership

- Every process needs an owner, supervisor, shutdown behavior, restart strategy,
  and observable name or lookup path when appropriate.
- Prefer supervised GenServer, Task.Supervisor, DynamicSupervisor, Registry,
  GenStage/Broadway, Oban, or existing abstractions over raw `spawn`.
- Avoid global process names unless the app already uses them and clustering
  behavior is understood.

## Message Protocols

- Keep call/cast/info message shapes explicit and versionable.
- Validate external messages before mutating state.
- Avoid unbounded mailboxes. Add backpressure, timeouts, batching, demand, or
  queue limits when producers can outpace consumers.
- Never block a GenServer on long CPU, network, or database work. Use supervised
  tasks or workers.

## State And Recovery

- Keep process state small, serializable, and reconstructable.
- Store durable state in the database or configured storage, not only in process
  memory, when data must survive restarts.
- Handle termination, monitor/down messages, retries, and cancellation according
  to local supervision strategy.
- Use `handle_continue/2` for startup work that should not block init.

## Timeouts And Ordering

- Set explicit timeouts for calls, tasks, external IO, and retries.
- Preserve ordering guarantees where clients rely on them.
- Test crash/restart paths, timeout paths, and duplicate message behavior.
