use std::sync::Mutex;
use std::time::Duration;

use axum::http::{HeaderMap, HeaderValue};
use serde_json::{Map, Value, json};

use super::*;
use crate::database_concurrency::PostgresCapacity;
use crate::database_concurrency::pool_budget::{StoredKeys, resolve_pool_budget};
use crate::postgres_pool::PoolSettings;

/// A frame publisher that records what it was asked to publish.
#[derive(Default)]
struct RecordingPublisher {
    published: Mutex<Vec<Vec<String>>>,
}

impl FramePublisher for RecordingPublisher {
    fn publish_frames(&self, _filter: &Value, frames: Vec<String>) -> Result<(), PublishRefused> {
        self.published
            .lock()
            .expect("publisher record")
            .push(frames);
        Ok(())
    }
}

/// A startup recorder that keeps every step.
#[derive(Default)]
struct RecordingStartup {
    steps: Mutex<Vec<(String, String, String)>>,
}

impl StartupRecorder for RecordingStartup {
    fn record_step(&self, family: &str, step: &str, outcome: &str) {
        self.steps.lock().expect("startup record").push((
            family.into(),
            step.into(),
            outcome.into(),
        ));
    }
}

#[derive(Debug, PartialEq, Eq)]
struct Marker(u32);

fn epoch(revision: i64, key: &str, value: Value) -> ConfigEpoch {
    let mut values = Map::new();
    values.insert(key.into(), value);
    ConfigEpoch {
        revision,
        active_values: Arc::new(values),
    }
}

fn lazy_pool() -> Pool {
    Pool::build(
        "postgresql://gobby_daemon_runtime@127.0.0.1:9/gobby",
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-family-test".into(),
            acquire_timeout: Duration::from_secs(1),
        },
    )
    .expect("a lazy pool builds without connecting")
}

fn budget() -> PoolBudget {
    resolve_pool_budget(
        &StoredKeys::default(),
        &PostgresCapacity {
            max_connections: 100,
            superuser_reserved_connections: 3,
            reserved_connections: 0,
        },
        8,
    )
    .expect("automatic keys resolve")
}

/// Context parts with no optional handle and a fresh configuration channel.
fn bare_parts(publisher: Arc<dyn FramePublisher>) -> ContextParts {
    let (config_publisher, config) = config_channel(ConfigEpoch::unpublished());
    ContextParts {
        publisher,
        backend: None,
        lease: None,
        budget: None,
        serve_pool: None,
        config,
        home: PathBuf::from("/fixture/home"),
        degraded: DegradedServices::default(),
        config_publisher,
        startup: None,
        extensions: Extensions::new(),
    }
}

#[tokio::test]
async fn context_exposes_each_handle() {
    let frames = Arc::new(RecordingPublisher::default());
    let (_backend_tx, backend) = watch::channel(BackendStatus::Starting { pid: 41 });
    let (_lease_tx, lease) = watch::channel(LeaseState::Active);
    let (config_publisher, config) = config_channel(epoch(7, "logging.level", json!("debug")));
    let pool = Arc::new(lazy_pool());
    let expected_budget = budget();
    let ctx = FamilyContext::new(ContextParts {
        publisher: frames.clone(),
        backend: Some(backend),
        lease: Some(lease),
        budget: Some(expected_budget.clone()),
        serve_pool: Some(Arc::clone(&pool)),
        config,
        home: PathBuf::from("/fixture/home"),
        degraded: DegradedServices::default(),
        config_publisher: Arc::clone(&config_publisher),
        startup: None,
        extensions: Extensions::new(),
    });

    assert_eq!(
        ctx.backend_status(),
        Some(BackendStatus::Starting { pid: 41 })
    );
    assert_eq!(
        ctx.subscribe_backend()
            .map(|receiver| receiver.borrow().clone()),
        Some(BackendStatus::Starting { pid: 41 })
    );
    assert_eq!(ctx.lease_state(), Some(LeaseState::Active));
    assert_eq!(ctx.budget(), Some(&expected_budget));
    assert!(
        ctx.serve_pool()
            .is_some_and(|served| Arc::ptr_eq(&served, &pool))
    );
    assert_eq!(ctx.home(), Path::new("/fixture/home"));
    assert_eq!(ctx.config_epoch().revision, 7);
    assert_eq!(ctx.subscribe_config().borrow().revision, 7);
    assert!(Arc::ptr_eq(&ctx.config_publisher(), &config_publisher));
    ctx.publisher()
        .publish_frames(&json!({"type": "fixture"}), vec!["frame".into()])
        .expect("the recording publisher accepts frames");
    assert_eq!(
        *frames.published.lock().expect("publisher record"),
        vec![vec!["frame".to_owned()]]
    );
}

#[tokio::test]
async fn config_subscribers_see_each_revision() {
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));
    let mut subscriber = ctx.subscribe_config();
    let publisher = ctx.config_publisher();

    reload_from(&publisher, epoch(1, "logging.level", json!("info"))).await;
    let first = tokio::time::timeout(Duration::from_secs(1), subscriber.changed()).await;
    assert!(
        matches!(first, Ok(Ok(()))),
        "revision 1 reaches the subscriber"
    );
    assert_eq!(subscriber.borrow_and_update().revision, 1);

    reload_from(&publisher, epoch(2, "logging.level", json!("debug"))).await;
    let second = tokio::time::timeout(Duration::from_secs(1), subscriber.changed()).await;
    assert!(
        matches!(second, Ok(Ok(()))),
        "revision 2 reaches the subscriber"
    );
    assert_eq!(subscriber.borrow_and_update().revision, 2);
    assert_eq!(ctx.config_epoch().revision, 2);

    // Reading the same stored epoch again changes nothing and notifies no one.
    reload_from(&publisher, epoch(2, "logging.level", json!("debug"))).await;
    assert!(!subscriber.has_changed().expect("the publisher is alive"));
}

/// Reloads `publisher` from a store whose read returns `stored`.
async fn reload_from(publisher: &ConfigEpochPublisher, stored: ConfigEpoch) -> bool {
    publisher
        .reload(|| async move { Ok::<_, std::convert::Infallible>(stored) })
        .await
        .unwrap_or_else(|never| match never {})
}

#[tokio::test]
async fn a_lower_stored_revision_read_through_reload_is_published() {
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));
    let mut subscriber = ctx.subscribe_config();
    let publisher = ctx.config_publisher();

    reload_from(&publisher, epoch(5, "logging.level", json!("warning"))).await;
    subscriber.mark_unchanged();
    // The store was restored to an older revision, so its revision moved backwards.
    let published = reload_from(&publisher, epoch(3, "logging.level", json!("info"))).await;

    assert!(published, "the restored epoch is reported as published");
    let current = ctx.config_epoch();
    assert_eq!(current.revision, 3);
    assert_eq!(
        current.active_values.get("logging.level"),
        Some(&json!("info"))
    );
    assert!(subscriber.has_changed().expect("the publisher is alive"));

    // A changed value at the same revision is published too.
    subscriber.mark_unchanged();
    reload_from(&publisher, epoch(3, "logging.level", json!("debug"))).await;
    assert_eq!(
        ctx.config_epoch().active_values.get("logging.level"),
        Some(&json!("debug"))
    );
    assert!(subscriber.has_changed().expect("the publisher is alive"));
}

/// A stored configuration row that a test changes between reads.
struct Store {
    current: Mutex<ConfigEpoch>,
    reads_started: std::sync::atomic::AtomicUsize,
}

impl Store {
    fn read_now(&self) -> ConfigEpoch {
        self.reads_started
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.current.lock().expect("store row").clone()
    }
}

#[tokio::test]
async fn competing_reloads_end_at_the_store_current_value() {
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));
    let publisher = ctx.config_publisher();
    let store = Arc::new(Store {
        current: Mutex::new(epoch(2, "logging.level", json!("debug"))),
        reads_started: std::sync::atomic::AtomicUsize::new(0),
    });

    // The first reload reads revision 2 and is held before it can publish.
    let (release_first, held) = tokio::sync::oneshot::channel::<()>();
    let (first_read, read_done) = tokio::sync::oneshot::channel::<()>();
    let first = tokio::spawn({
        let publisher = Arc::clone(&publisher);
        let store = Arc::clone(&store);
        async move {
            publisher
                .reload(|| async move {
                    let stored = store.read_now();
                    let _ = first_read.send(());
                    let _ = held.await;
                    Ok::<_, std::convert::Infallible>(stored)
                })
                .await
        }
    });
    read_done.await.expect("the first read ran");

    // The store is restored to revision 1, and a second reload starts.
    *store.current.lock().expect("store row") = epoch(1, "logging.level", json!("info"));
    let second = tokio::spawn({
        let publisher = Arc::clone(&publisher);
        let store = Arc::clone(&store);
        async move {
            publisher
                .reload(|| async move { Ok::<_, std::convert::Infallible>(store.read_now()) })
                .await
        }
    });
    for _ in 0..16 {
        tokio::task::yield_now().await;
    }
    assert_eq!(
        store
            .reads_started
            .load(std::sync::atomic::Ordering::SeqCst),
        1,
        "the second read waits for the first reload to publish"
    );

    release_first.send(()).expect("the first reload is waiting");
    let _ = first.await.expect("first reload");
    let _ = second.await.expect("second reload");

    let current = ctx.config_epoch();
    assert_eq!(current.revision, 1);
    assert_eq!(
        current.active_values.get("logging.level"),
        Some(&json!("info"))
    );
}

#[test]
fn degraded_set_reads_back_marked_names() {
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));

    ctx.degraded().mark("x");
    ctx.degraded().mark("a");
    assert!(ctx.degraded().contains("x"));
    assert_eq!(ctx.degraded().names(), vec!["a".to_owned(), "x".to_owned()]);

    ctx.degraded().clear("x");
    assert!(!ctx.degraded().contains("x"));
    assert_eq!(ctx.degraded().names(), vec!["a".to_owned()]);
}

#[test]
fn logger_target_is_the_family_namespace() {
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));

    assert_eq!(ctx.logger("terminal_ws").target(), "gobby.terminal_ws");
}

#[test]
fn family_logger_counter_reads_back_with_family_label() {
    // The counter and family names are unique to this test, so other tests that share
    // the process registry never touch this series.
    const COUNTER: &str = "family_logger_probe_total";
    let ctx = FamilyContext::new(bare_parts(Arc::new(RecordingPublisher::default())));

    ctx.logger("logger_probe").inc_counter(COUNTER, 2);

    let series: Vec<_> = crate::telemetry::Telemetry::global()
        .snapshot()
        .into_iter()
        .filter(|sample| sample.name == COUNTER)
        .collect();
    assert_eq!(
        series,
        vec![crate::telemetry::Sample {
            name: COUNTER,
            labels: vec![("family", "logger_probe".to_owned())],
            value: crate::telemetry::SampleValue::Counter(2),
        }]
    );
}

#[test]
fn identity_reads_three_headers_and_has_no_secret_field() {
    let mut headers = HeaderMap::new();
    headers.insert("x-gobby-user-id", HeaderValue::from_static("user-1"));
    headers.insert("x-gobby-machine-id", HeaderValue::from_static("machine-1"));
    headers.insert("x-gobby-key-id", HeaderValue::from_static("key-1"));
    headers.insert(
        "x-gobby-front-door",
        HeaderValue::from_static("front-door-secret"),
    );

    let identity = CallerIdentity::from_headers(&headers);
    assert_eq!(
        identity,
        Some(CallerIdentity {
            user_id: "user-1".into(),
            machine_id: "machine-1".into(),
            key_id: "key-1".into(),
        })
    );
    assert!(!format!("{identity:?}").contains("front-door-secret"));

    headers.remove("x-gobby-user-id");
    assert_eq!(CallerIdentity::from_headers(&headers), None);
}

#[test]
fn extension_and_recorder_round_trip() {
    let recorder = Arc::new(RecordingStartup::default());
    let mut parts = bare_parts(Arc::new(RecordingPublisher::default()));
    parts.extensions.insert(Marker(7));
    parts.startup = Some(recorder.clone());
    let ctx = FamilyContext::new(parts);

    assert_eq!(ctx.extension::<Marker>().as_deref(), Some(&Marker(7)));
    assert_eq!(ctx.extension::<String>(), None);
    let attached = ctx.startup();
    assert!(attached.is_some(), "the recorder reaches the context");
    if let Some(attached) = attached {
        attached.record_step("fixture", "schema", "ok");
    }
    assert_eq!(
        *recorder.steps.lock().expect("startup record"),
        vec![("fixture".to_owned(), "schema".to_owned(), "ok".to_owned())]
    );
}

#[test]
fn unconfigured_context_is_empty() {
    let ctx = FamilyContext::unconfigured();

    assert_eq!(ctx.backend_status(), None);
    assert_eq!(ctx.lease_state(), None);
    assert_eq!(ctx.budget(), None);
    assert!(ctx.serve_pool().is_none());
    assert!(ctx.startup().is_none());
    assert_eq!(ctx.config_epoch().revision, 0);
    assert!(ctx.config_epoch().active_values.is_empty());
    assert!(ctx.degraded().names().is_empty());
    assert_eq!(
        ctx.publisher()
            .publish_frames(&json!({"type": "fixture"}), vec!["frame".into()]),
        Err(PublishRefused)
    );
}
