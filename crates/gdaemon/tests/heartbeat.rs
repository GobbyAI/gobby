use std::future::pending;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::{Result, bail};
use gobby_daemon::heartbeat::HeartbeatHost;
use tokio::time::{Instant, advance, sleep};

async fn settle() {
    for _ in 0..10 {
        tokio::task::yield_now().await;
    }
}

async fn tick(seconds: u64) {
    advance(Duration::from_secs(seconds)).await;
    settle().await;
}

fn counter(host: &mut HeartbeatHost, name: &str) -> Result<Arc<AtomicUsize>> {
    let count = Arc::new(AtomicUsize::new(0));
    let runs = Arc::clone(&count);
    host.register(name, Duration::from_secs(5), move || {
        runs.fetch_add(1, Ordering::SeqCst);
        async { Ok(()) }
    })?;
    Ok(count)
}

#[tokio::test(start_paused = true)]
async fn delayed_intervals_preserve_spacing_without_catch_up_bursts() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let starts = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&starts);
    host.register("seven seconds", Duration::from_secs(7), move || {
        recorded.lock().unwrap().push(Instant::now());
        async { Ok(()) }
    })?;
    host.start()?;
    settle().await;
    tick(5).await;
    assert!(starts.lock().unwrap().is_empty());
    tick(5).await;
    tick(10).await;
    tick(100).await;
    assert_eq!(
        starts.lock().unwrap().len(),
        3,
        "one run after a large delay"
    );
    tick(5).await;
    assert_eq!(starts.lock().unwrap().len(), 3);
    tick(5).await;
    let times = starts.lock().unwrap().clone();
    assert_eq!(times.len(), 4);
    assert!(
        times
            .windows(2)
            .all(|pair| pair[1] - pair[0] >= Duration::from_secs(7))
    );
    host.stop().await
}

#[tokio::test(start_paused = true)]
async fn cadence_is_start_to_start_and_jobs_never_overlap() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let starts = Arc::new(Mutex::new(Vec::new()));
    let active = Arc::new(AtomicUsize::new(0));
    let overlaps = Arc::new(AtomicUsize::new(0));
    let sibling_runs = Arc::new(AtomicUsize::new(0));
    let finish = Arc::new(tokio::sync::Notify::new());
    let recorded = Arc::clone(&starts);
    let running = Arc::clone(&active);
    let completed = Arc::clone(&finish);
    let concurrent = Arc::clone(&overlaps);
    host.register("slow", Duration::from_secs(10), move || {
        let recorded = Arc::clone(&recorded);
        let running = Arc::clone(&running);
        let completed = Arc::clone(&completed);
        let concurrent = Arc::clone(&concurrent);
        async move {
            if running.fetch_add(1, Ordering::SeqCst) != 0 {
                concurrent.fetch_add(1, Ordering::SeqCst);
            }
            recorded.lock().unwrap().push(Instant::now());
            completed.notified().await;
            running.fetch_sub(1, Ordering::SeqCst);
            Ok(())
        }
    })?;
    let observed = Arc::clone(&active);
    let concurrent = Arc::clone(&overlaps);
    let siblings = Arc::clone(&sibling_runs);
    host.register("sibling", Duration::from_secs(5), move || {
        siblings.fetch_add(1, Ordering::SeqCst);
        if observed.load(Ordering::SeqCst) != 0 {
            concurrent.fetch_add(1, Ordering::SeqCst);
        }
        async { Ok(()) }
    })?;
    host.start()?;
    settle().await;
    tick(10).await;
    tick(6).await;
    finish.notify_one();
    settle().await;
    tick(4).await;
    let times = starts.lock().unwrap().clone();
    assert_eq!(times.len(), 2);
    assert_eq!(times[1] - times[0], Duration::from_secs(10));
    tick(6).await;
    finish.notify_one();
    settle().await;
    assert!(sibling_runs.load(Ordering::SeqCst) >= 2);
    assert_eq!(overlaps.load(Ordering::SeqCst), 0, "jobs run sequentially");
    assert_eq!(active.load(Ordering::SeqCst), 0);
    host.stop().await
}

#[tokio::test(start_paused = true)]
async fn failing_job_leaves_siblings_running() -> Result<()> {
    let mut host = HeartbeatHost::new();
    host.register("failure", Duration::from_secs(5), || async {
        bail!("expected failure")
    })?;
    let sibling = counter(&mut host, "sibling")?;
    host.start()?;
    settle().await;
    tick(5).await;
    tick(5).await;
    assert_eq!(sibling.load(Ordering::SeqCst), 2);
    host.stop().await
}

#[tokio::test(start_paused = true)]
async fn panicking_job_runs_again_and_leaves_siblings_running() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let attempts = Arc::new(AtomicUsize::new(0));
    let calls = Arc::clone(&attempts);
    host.register("panicker", Duration::from_secs(5), move || {
        // Panic in the factory, before it even returns a future.
        assert_ne!(calls.fetch_add(1, Ordering::SeqCst), 0, "expected panic");
        async { Ok(()) }
    })?;
    let sibling = counter(&mut host, "sibling")?;
    host.start()?;
    settle().await;
    tick(5).await;
    tick(5).await;
    assert_eq!(attempts.load(Ordering::SeqCst), 2);
    assert_eq!(sibling.load(Ordering::SeqCst), 2);
    host.stop().await
}

#[test]
fn panic_is_logged_with_the_registrant_name() -> Result<()> {
    let output = std::process::Command::new(std::env::current_exe()?)
        .args([
            "--exact",
            "panicking_job_runs_again_and_leaves_siblings_running",
            "--nocapture",
        ])
        .output()?;
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let stderr = String::from_utf8(output.stderr)?;
    assert!(
        stderr.contains("gdaemon heartbeat job panicker failed:"),
        "{stderr}"
    );
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn duplicate_names_and_registration_after_start_are_rejected() -> Result<()> {
    let mut host = HeartbeatHost::new();
    counter(&mut host, "one")?;
    assert!(
        counter(&mut host, "one")
            .unwrap_err()
            .to_string()
            .contains("duplicate")
    );
    assert!(
        host.register("zero", Duration::ZERO, || async { Ok(()) })
            .is_err()
    );
    host.start()?;
    assert!(
        counter(&mut host, "two")
            .unwrap_err()
            .to_string()
            .contains("frozen")
    );
    assert!(host.start().is_err());
    host.stop().await?;
    assert!(counter(&mut host, "three").is_err());
    host.stop().await
}

#[test]
fn stopping_after_a_supervisor_join_error_is_idempotent() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    {
        let _entered = runtime.enter();
        host.start()?;
    }
    drop(runtime);

    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(async {
        let error = host.stop().await.unwrap_err();
        assert!(error.downcast_ref::<tokio::task::JoinError>().is_some());
        assert!(host.stop().await.is_ok());
    });
    Ok(())
}

struct Active(Arc<AtomicUsize>);

impl Drop for Active {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

fn blocking_job(
    host: &mut HeartbeatHost,
    finish_after: Option<Duration>,
) -> Result<Arc<AtomicUsize>> {
    let active = Arc::new(AtomicUsize::new(0));
    let running = Arc::clone(&active);
    host.register("in flight", Duration::from_secs(5), move || {
        let running = Arc::clone(&running);
        async move {
            running.fetch_add(1, Ordering::SeqCst);
            let _active = Active(running);
            match finish_after {
                Some(duration) => sleep(duration).await,
                None => pending().await,
            }
            Ok(())
        }
    })?;
    Ok(active)
}

#[tokio::test(start_paused = true)]
async fn shutdown_drains_in_flight_work_and_stops_scheduling() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let active = blocking_job(&mut host, Some(Duration::from_secs(4)))?;
    let sibling = counter(&mut host, "sibling")?;
    host.start()?;
    settle().await;
    tick(5).await;
    assert_eq!(active.load(Ordering::SeqCst), 1);
    let stopped = tokio::spawn(async move { host.stop().await });
    settle().await;
    assert!(!stopped.is_finished());
    tick(4).await;
    stopped.await??;
    assert_eq!(active.load(Ordering::SeqCst), 0);
    tick(30).await;
    assert_eq!(sibling.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn shutdown_aborts_and_joins_stragglers_after_five_seconds() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let active = blocking_job(&mut host, None)?;
    let sibling = counter(&mut host, "sibling")?;
    host.start()?;
    settle().await;
    tick(5).await;
    let stopped = tokio::spawn(async move { host.stop().await });
    settle().await;
    tick(4).await;
    assert!(!stopped.is_finished());
    assert_eq!(active.load(Ordering::SeqCst), 1);
    tick(1).await;
    stopped.await??;
    assert_eq!(active.load(Ordering::SeqCst), 0);
    tick(30).await;
    assert_eq!(sibling.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn dropping_host_aborts_its_active_child() -> Result<()> {
    let mut host = HeartbeatHost::new();
    let active = blocking_job(&mut host, None)?;
    host.start()?;
    settle().await;
    tick(5).await;
    assert_eq!(active.load(Ordering::SeqCst), 1);
    drop(host);
    settle().await;
    assert_eq!(active.load(Ordering::SeqCst), 0);
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn zero_job_serve_stops_cleanly() -> Result<()> {
    let auth = std::sync::Arc::new(
        gobby_daemon::front_door::auth::AuthState::new(
            "test-secret".into(),
            None,
            Default::default(),
        )
        .expect("auth"),
    );
    let result =
        gobby_daemon::serve::serve(Vec::new(), &Default::default(), None, auth, async {}).await;
    assert!(result.is_ok(), "{result:?}");
    Ok(())
}
