//! Family start tests: the pool fold, the start-failure disposition, the configuration
//! poller and the start unwind. Time is paused, so no test waits on the wall clock.

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};

use axum::Router;
use gobby_core::database_concurrency::PostgresCapacity;
use gobby_core::database_concurrency::pool_budget::{StoredKeys, resolve_pool_budget};
use gobby_core::family::lifecycle::{HookError, StopFailure, StopMode};
use gobby_core::family::{PublishRefused, config_channel};
use gobby_core::route_family::BoxFut;
use serde_json::{Map, Value, json};
use tokio::sync::Notify;
use tracing::Level;
use tracing_subscriber::Registry;
use tracing_subscriber::layer::SubscriberExt;

use super::*;

/// The poll interval every poller test uses.
const EVERY: Duration = Duration::from_secs(1);

/// How long a test waits for the poller's next publication before it counts none.
const QUIET: Duration = Duration::from_secs(30);

/// 100 connections with 3 reserved: a pool budget of 72.
fn capacity() -> PostgresCapacity {
    PostgresCapacity {
        max_connections: 100,
        superuser_reserved_connections: 3,
        reserved_connections: 0,
    }
}

/// The budget the resolver gives for a stored `pool_max_size` on 100 connections.
fn budget_for(pool_max_size: Option<&str>) -> PoolBudget {
    let stored = StoredKeys {
        pool_max_size: pool_max_size.map(str::to_owned),
        ..StoredKeys::default()
    };
    resolve_pool_budget(&stored, &capacity(), 8).expect("the stored keys resolve")
}

fn epoch(revision: i64, values: Value) -> ConfigEpoch {
    let Value::Object(active_values) = values else {
        panic!("active values are an object");
    };
    ConfigEpoch {
        revision,
        active_values: Arc::new(active_values),
    }
}

/// A source that answers its script in order, `None` being a failed read, and repeats
/// its last answer once the script runs out. It counts its reads.
struct Scripted {
    script: Vec<Option<ConfigEpoch>>,
    reads: AtomicUsize,
}

impl Scripted {
    fn new(script: Vec<Option<ConfigEpoch>>) -> Arc<Self> {
        Arc::new(Self {
            script,
            reads: AtomicUsize::new(0),
        })
    }

    fn reads(&self) -> usize {
        self.reads.load(Ordering::SeqCst)
    }
}

impl ConfigEpochSource for Scripted {
    fn read(&self) -> Pin<Box<dyn Future<Output = anyhow::Result<ConfigEpoch>> + Send + '_>> {
        let index = self.reads.fetch_add(1, Ordering::SeqCst);
        let answer = self
            .script
            .get(index)
            .or(self.script.last())
            .cloned()
            .flatten();
        Box::pin(async move { answer.ok_or_else(|| anyhow::anyhow!("hub unreachable")) })
    }
}

/// Every epoch `rx` is moved to, in order, until no publication arrives for [`QUIET`].
async fn published(rx: &mut watch::Receiver<ConfigEpoch>) -> Vec<(i64, Map<String, Value>)> {
    let mut seen = Vec::new();
    while let Ok(Ok(())) = tokio::time::timeout(QUIET, rx.changed()).await {
        let current = rx.borrow_and_update();
        seen.push((current.revision, (*current.active_values).clone()));
    }
    seen
}

fn revisions(seen: &[(i64, Map<String, Value>)]) -> Vec<i64> {
    seen.iter().map(|(revision, _)| *revision).collect()
}

/// Lets paused time run forward by `span`, so every timer inside it fires.
async fn let_time_pass(span: Duration) {
    let _ = tokio::time::timeout(span, std::future::pending::<()>()).await;
}

/// Records the level of every event whose target is in the `gobby` namespace.
#[derive(Clone, Default)]
struct Levels(Arc<Mutex<Vec<Level>>>);

impl Levels {
    fn count(&self, level: Level) -> usize {
        self.0
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|seen| **seen == level)
            .count()
    }
}

impl<S: tracing::Subscriber> tracing_subscriber::Layer<S> for Levels {
    fn on_event(
        &self,
        event: &tracing::Event<'_>,
        _context: tracing_subscriber::layer::Context<'_, S>,
    ) {
        let target = event.metadata().target();
        if target == "gobby" || target.starts_with("gobby.") {
            self.0
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .push(*event.metadata().level());
        }
    }
}

#[test]
fn fold_maps_refusal_to_fatal_and_degradation_to_degraded() {
    let refused = fold_outcome(Some(PoolOutcome::Refused {
        message: "pool_max_size exceeds the budget".into(),
    }));
    assert_eq!(
        refused.err(),
        Some(StartFailure {
            family: "database_pool",
            message: "pool_max_size exceeds the budget".into(),
            relaunch_could_clear: false,
        })
    );

    let degraded = fold_outcome(Some(PoolOutcome::Degraded {
        reason: "database_pool",
    }));
    assert_eq!(
        degraded.ok().map(|state| state.degraded),
        Some(Some("database_pool"))
    );

    let no_database = fold_outcome(None);
    assert_eq!(
        no_database.ok(),
        Some(PoolState {
            budget: None,
            degraded: None,
        })
    );
}

#[test]
fn budget_below_eight_marks_database_pool_degraded() {
    let short = budget_for(Some("66"));
    assert_eq!(short.native_budget, 6, "the fixture gives a budget of six");
    let folded = fold_outcome(Some(PoolOutcome::Ready(short.clone())));
    assert_eq!(
        folded.ok(),
        Some(PoolState {
            budget: Some(short),
            degraded: Some("database_pool"),
        })
    );

    let full = budget_for(None);
    assert_eq!(full.native_budget, 8, "the fixture gives a budget of eight");
    let folded = fold_outcome(Some(PoolOutcome::Ready(full.clone())));
    assert_eq!(
        folded.ok(),
        Some(PoolState {
            budget: Some(full),
            degraded: None,
        })
    );
}

#[test]
fn disposition_refuses_when_relaunch_cannot_clear() {
    let sticky = StartFailure {
        family: "fixture",
        message: "a stored value no relaunch clears".into(),
        relaunch_could_clear: false,
    };
    assert!(
        matches!(disposition(&sticky), Disposition::Refuse { .. }),
        "a failure no relaunch clears refuses: {:?}",
        disposition(&sticky)
    );

    let transient = StartFailure {
        relaunch_could_clear: true,
        ..sticky
    };
    assert_eq!(disposition(&transient), Disposition::Exit);

    let pool_refusal = fold_outcome(Some(PoolOutcome::Refused {
        message: "stored value refused".into(),
    }))
    .err()
    .map(|failure| disposition(&failure));
    let reason = match pool_refusal {
        Some(Disposition::Refuse { reason }) => Some(reason),
        _ => None,
    };
    assert!(
        reason
            .as_deref()
            .is_some_and(|reason| reason.contains("database_pool")),
        "a refused pool refuses, naming database_pool: {reason:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn poller_publishes_only_when_the_epoch_changes() {
    let (publisher, mut rx) = config_channel(ConfigEpoch::unpublished());
    let source = Scripted::new(vec![
        Some(epoch(1, json!({}))),
        Some(epoch(1, json!({}))),
        Some(epoch(2, json!({}))),
    ]);
    let poller = spawn_config_poller(source.clone(), Arc::clone(&publisher), EVERY);

    let seen = published(&mut rx).await;
    poller.abort();

    assert_eq!(revisions(&seen), [1, 2]);
    assert!(
        source.reads() >= 3,
        "the poller read {} times",
        source.reads()
    );
}

#[tokio::test(start_paused = true)]
async fn poller_publishes_a_restored_lower_revision_and_changed_values() {
    let (publisher, mut rx) = config_channel(ConfigEpoch::unpublished());
    let source = Scripted::new(vec![
        Some(epoch(5, json!({"logging.level": "warning"}))),
        Some(epoch(3, json!({"logging.level": "warning"}))),
        Some(epoch(3, json!({"logging.level": "info"}))),
    ]);
    let poller = spawn_config_poller(source, Arc::clone(&publisher), EVERY);

    let seen = published(&mut rx).await;
    poller.abort();

    let level = |values: &Map<String, Value>| values.get("logging.level").cloned();
    let seen: Vec<(i64, Option<Value>)> = seen
        .iter()
        .map(|(revision, values)| (*revision, level(values)))
        .collect();
    assert_eq!(
        seen,
        [
            (5, Some(json!("warning"))),
            (3, Some(json!("warning"))),
            (3, Some(json!("info"))),
        ]
    );
}

#[tokio::test(start_paused = true)]
async fn poller_reads_only_under_the_publisher_lock() {
    let (publisher, rx) = config_channel(ConfigEpoch::unpublished());
    let release = Arc::new(Notify::new());
    let held = {
        let publisher = Arc::clone(&publisher);
        let release = Arc::clone(&release);
        tokio::spawn(async move {
            publisher
                .reload(|| async move {
                    release.notified().await;
                    Ok::<_, anyhow::Error>(epoch(7, json!({})))
                })
                .await
        })
    };
    tokio::task::yield_now().await;
    let source = Scripted::new(vec![Some(epoch(8, json!({})))]);
    let poller = spawn_config_poller(source.clone(), Arc::clone(&publisher), EVERY);

    let_time_pass(EVERY * 5).await;
    let reads_while_held = source.reads();
    release.notify_one();
    let held = held.await.expect("the held reload finishes");
    let_time_pass(EVERY * 5).await;
    poller.abort();

    assert_eq!(held.ok(), Some(true), "the held reload publishes its read");
    assert_eq!(
        reads_while_held, 0,
        "the poller read while the lock was held"
    );
    assert_eq!(
        rx.borrow().revision,
        8,
        "the poller's read follows the held one"
    );
}

#[tokio::test(start_paused = true)]
async fn failed_poll_keeps_epoch_and_warns_once() {
    let levels = Levels::default();
    let _logs = tracing::subscriber::set_default(Registry::default().with(levels.clone()));
    let (publisher, mut rx) = config_channel(ConfigEpoch::unpublished());
    let source = Scripted::new(vec![
        Some(epoch(1, json!({}))),
        None,
        None,
        None,
        Some(epoch(2, json!({}))),
    ]);
    let poller = spawn_config_poller(source.clone(), Arc::clone(&publisher), EVERY);

    let seen = published(&mut rx).await;
    poller.abort();

    assert_eq!(revisions(&seen), [1, 2], "the failed polls publish nothing");
    assert!(
        source.reads() >= 5,
        "the poller read {} times",
        source.reads()
    );
    assert_eq!(levels.count(Level::WARN), 1, "one warning on the failure");
    assert_eq!(levels.count(Level::INFO), 1, "one notice on the recovery");
    assert_eq!(levels.count(Level::DEBUG), 2, "the repeated failures");
    assert_eq!(levels.count(Level::ERROR), 0);
}

/// A frame publisher with no subscribers.
struct NoFrames;

impl FramePublisher for NoFrames {
    fn publish_frames(&self, _filter: &Value, _frames: Vec<String>) -> Result<(), PublishRefused> {
        Err(PublishRefused)
    }
}

/// A family that starts at once and takes a second to deactivate.
struct StopsSlowly {
    stopped: AtomicBool,
}

impl RouteFamily<FrontDoorState> for StopsSlowly {
    fn name(&self) -> &'static str {
        "fixture_stops_slowly"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn deactivate<'a>(&'a self, _mode: StopMode) -> BoxFut<'a, Result<(), StopFailure>> {
        Box::pin(async move {
            tokio::time::sleep(Duration::from_secs(1)).await;
            self.stopped.store(true, Ordering::SeqCst);
            Ok(())
        })
    }
}

/// A family whose start runs for an hour and then fails fatally.
struct StartsSlowly;

impl RouteFamily<FrontDoorState> for StartsSlowly {
    fn name(&self) -> &'static str {
        "fixture_starts_slowly"
    }

    fn prefixes(&self) -> &'static [&'static str] {
        &[]
    }

    fn router(&self) -> Router<FrontDoorState> {
        Router::new()
    }

    fn start_rank(&self) -> u8 {
        1
    }

    fn start<'a>(&'a self, _ctx: &'a FamilyContext) -> BoxFut<'a, Result<(), HookError>> {
        Box::pin(async {
            tokio::time::sleep(Duration::from_secs(3600)).await;
            Err(HookError::Fatal {
                message: "the fixture refuses to start".into(),
                relaunch_could_clear: false,
            })
        })
    }
}

#[tokio::test(start_paused = true)]
async fn start_unwind_budget_begins_when_the_unwind_begins() {
    let stops: &'static StopsSlowly = Box::leak(Box::new(StopsSlowly {
        stopped: AtomicBool::new(false),
    }));
    let starts: &'static StartsSlowly = Box::leak(Box::new(StartsSlowly));
    let routes = BTreeMap::from([
        (stops.name().to_owned(), RouteBackend::Native),
        (starts.name().to_owned(), RouteBackend::Native),
    ]);
    let inputs = PrepareInputs {
        serve_pool: None,
        home: PathBuf::from("/nonexistent/gobby-home"),
        routes,
        publisher: Arc::new(NoFrames),
        backend: None,
        lease: None,
        epoch_source: Scripted::new(vec![Some(epoch(1, json!({})))]),
        cpu_count: 8,
        families: vec![stops, starts],
        extensions: Extensions::new(),
        startup: None,
    };

    let failure = prepare(inputs).await.err();

    assert_eq!(
        failure.map(|failure| failure.family),
        Some("fixture_starts_slowly"),
        "the slow start's fatal failure is the start failure"
    );
    assert!(
        stops.stopped.load(Ordering::SeqCst),
        "the started family finished its deactivate inside the unwind budget"
    );
}
