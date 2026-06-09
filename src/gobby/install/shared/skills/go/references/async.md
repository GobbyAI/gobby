# Go Concurrency Reference

Use concurrency only when it simplifies ownership or improves observable latency. A sequential loop is usually easier to test, cancel, and debug than a goroutine graph.

## Context

- Accept `context.Context` as the first parameter for request, job, network, database, subprocess, and file-watching work.
- Never store contexts in structs unless the struct itself represents a request-scoped operation.
- Check `ctx.Err()` after blocking calls when the caller needs cancellation-specific behavior.
- Prefer `context.WithTimeout` at the boundary that owns the service-level objective.

## Goroutine Ownership

Every goroutine needs an owner, an exit condition, and an error path.

```go
group, ctx := errgroup.WithContext(ctx)
group.SetLimit(8)

for _, item := range items {
    item := item
    group.Go(func() error {
        return process(ctx, item)
    })
}

if err := group.Wait(); err != nil {
    return fmt.Errorf("process items: %w", err)
}
```

Avoid fire-and-forget goroutines. When detaching work is required, log or report failures and make shutdown behavior explicit.

## Channels

- Close channels from the sending side.
- Use buffered channels to model bounded capacity, not to hide blocked receivers.
- Prefer a return value or callback for one-shot results.
- Use `select` with `ctx.Done()` for loops that can block forever.

## Timers And Tickers

Stop timers and tickers when done. Drain timer channels only when required by the timer pattern in use. Tests that involve time should control the clock or use short deterministic synchronization points.

## Shared State

- Prefer local ownership and message passing for simple pipelines.
- Use `sync.Mutex` for shared mutable state when ownership is clearer than channel choreography.
- Use atomics only for narrow counters, flags, or lock-free paths with tests that exercise races.
- Run `go test -race` for changes to goroutines, locks, timers, shared maps, or cancellation paths.
