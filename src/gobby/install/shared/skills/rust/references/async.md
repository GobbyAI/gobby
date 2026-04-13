# Async — Reference

## Task Spawning

`tokio::spawn` requires `Send + 'static` — the future must be safe to move between threads and own all its data:

```rust
let handle = tokio::spawn(async move {
    let result = fetch_data(url).await?;
    process(result).await
});

// Collect the result
let output = handle.await??;
```

## spawn_blocking for CPU Work

Never do heavy computation in an async task — it blocks the runtime:

```rust
let hash = tokio::task::spawn_blocking(move || {
    argon2::hash_password(password.as_bytes(), &salt)
}).await?;
```

## Timeouts

Wrap every external I/O call:

```rust
use tokio::time::{timeout, Duration};

let response = timeout(Duration::from_secs(10), client.get(url).send())
    .await
    .map_err(|_| anyhow!("request timed out after 10s"))??;
```

## select! with Cancellation Safety

```rust
use tokio::select;

loop {
    select! {
        msg = rx.recv() => {
            match msg {
                Some(m) => handle(m).await,
                None => break, // channel closed
            }
        }
        _ = tokio::time::sleep(Duration::from_secs(60)) => {
            // Periodic maintenance
            cleanup().await;
        }
        _ = shutdown.recv() => {
            info!("shutting down");
            break;
        }
    }
}
```

**Cancellation safety**: when `select!` picks one branch, the other futures are dropped. `rx.recv()` on `tokio::mpsc` is cancellation-safe (no message lost). `tokio::io::AsyncReadExt::read` is **not** — a partial read may be lost. Use `tokio::io::AsyncBufReadExt::read_line` or buffer manually.

## Graceful Shutdown

```rust
use tokio::signal;
use tokio::sync::broadcast;

#[tokio::main]
async fn main() -> Result<()> {
    let (shutdown_tx, _) = broadcast::channel::<()>(1);

    let worker = {
        let mut shutdown_rx = shutdown_tx.subscribe();
        tokio::spawn(async move {
            loop {
                select! {
                    work = get_next_job() => { process(work).await; }
                    _ = shutdown_rx.recv() => {
                        info!("worker: draining...");
                        drain_queue().await;
                        break;
                    }
                }
            }
        })
    };

    signal::ctrl_c().await?;
    info!("shutdown signal received");
    let _ = shutdown_tx.send(());
    worker.await?;
    Ok(())
}
```

## Send + Sync Pitfalls

Holding a non-Send type across `.await` prevents the future from being `Send`:

```rust
// Bad: MutexGuard held across await
async fn bad_example(mutex: &tokio::sync::Mutex<Vec<String>>) {
    let mut guard = mutex.lock().await;
    guard.push(fetch_name().await); // guard held across await!
}

// Good: drop guard before await
async fn good_example(mutex: &tokio::sync::Mutex<Vec<String>>) {
    let name = fetch_name().await;
    let mut guard = mutex.lock().await;
    guard.push(name);
}
```

Same applies to `Rc`, `RefCell`, and `std::sync::MutexGuard` — none are `Send`. If you see "future is not Send", check what's alive across an `.await` point.
