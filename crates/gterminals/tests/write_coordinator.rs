use std::collections::VecDeque;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_terminals::leases::{self, Registry, Viewer};
use gobby_terminals::repository::{self, Backend, Ownership, State, Terminal};
use gobby_terminals::write::{
    BatchOp, BatchResult, BatchRuntime, BatchTarget, BoxFuture, Coordinator, Delivery, Kind,
    LeaseClaim, Origin, Outcome, Refusal, Runtime, Runtimes, SequenceRequest, SequenceStep, Stage,
    Step, WakeRequest, WakeStep, WriteFailure, WriteRequest,
};
use serde_json::json;
use tokio::sync::oneshot;
use uuid::Uuid;

const TEST_HUB: &str = "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test";

struct Hub {
    url: String,
    schema: String,
    pool: Arc<Pool>,
}

impl Hub {
    async fn new() -> anyhow::Result<Self> {
        let url = std::env::var("GOBBY_TERMINALS_TEST_DATABASE_URL")?;
        anyhow::ensure!(
            url == TEST_HUB,
            "write coordinator tests require the explicitly selected isolated test hub"
        );
        let schema = format!("gobby_terminal_write_{}", Uuid::new_v4().simple());
        let admin_url = url.clone();
        let owned_schema = schema.clone();
        tokio::task::spawn_blocking(move || {
            let baseline = include_str!("../../gcore/assets/schema/baseline.sql");
            let terminal_ddl = baseline
                .split_once("CREATE TABLE terminals (")
                .expect("canonical terminal table")
                .1
                .split_once("\n);")
                .expect("terminal table end")
                .0;
            let mut admin = gobby_core::postgres::connect_readwrite(&admin_url)?;
            admin.batch_execute(&format!(
                "SET statement_timeout = 5000;
                 CREATE SCHEMA {owned_schema};
                 CREATE TABLE {owned_schema}.terminals ({terminal_ddl}\n);
                 ALTER TABLE {owned_schema}.terminals ADD PRIMARY KEY (id);
                 GRANT USAGE ON SCHEMA {owned_schema} TO gobby_daemon_runtime;
                 GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {owned_schema}
                    TO gobby_daemon_runtime;"
            ))?;
            anyhow::Ok(())
        })
        .await??;
        let pool = Pool::build(
            &format!("{url}?options=-c%20search_path%3D{schema}"),
            PoolSettings {
                max_size: 4,
                application_name: "gobby-gdaemon-terminal-write-test".into(),
                acquire_timeout: Duration::from_secs(5),
            },
        )?;
        Ok(Self {
            url,
            schema,
            pool: Arc::new(pool),
        })
    }

    async fn insert(&self, backend: Backend) -> anyhow::Result<Uuid> {
        let terminal = terminal(backend);
        self.pool
            .transaction(None, async |tx| {
                repository::insert(tx, &terminal).await?;
                anyhow::Ok(())
            })
            .await?;
        Ok(terminal.id)
    }

    async fn get(&self, id: Uuid) -> anyhow::Result<Terminal> {
        self.pool
            .transaction(None, async |tx| {
                Ok(repository::get(tx, id).await?.expect("stored terminal"))
            })
            .await
    }

    async fn unresolved(&self, id: Uuid) -> anyhow::Result<Vec<String>> {
        let row = self.get(id).await?;
        Ok(row
            .unresolved_writes
            .as_object()
            .expect("unresolved map")
            .keys()
            .cloned()
            .collect())
    }

    async fn quarantine_key(&self, id: Uuid) -> anyhow::Result<Option<String>> {
        let row = self.get(id).await?;
        assert_eq!(
            row.automatic_write_quarantined_at.is_some(),
            row.automatic_write_quarantine_action_key.is_some(),
            "quarantine columns move together"
        );
        Ok(row.automatic_write_quarantine_action_key)
    }
}

impl Drop for Hub {
    fn drop(&mut self) {
        let url = self.url.clone();
        let schema = self.schema.clone();
        let cleanup = std::thread::spawn(move || {
            let mut admin = gobby_core::postgres::connect_readwrite(&url)?;
            admin.batch_execute(&format!(
                "SET statement_timeout = 5000; DROP SCHEMA IF EXISTS {schema} CASCADE"
            ))?;
            anyhow::Ok(())
        })
        .join();
        if !std::thread::panicking() {
            cleanup
                .expect("schema cleanup thread")
                .expect("schema cleanup");
        }
    }
}

fn terminal(backend: Backend) -> Terminal {
    let now: DateTime<Utc> = "2026-10-01T12:00:00Z".parse().expect("fixture timestamp");
    Terminal {
        id: Uuid::new_v4(),
        backend,
        ownership: Ownership::Gobby,
        state: State::Pending,
        spawn_key: Some(Uuid::new_v4().to_string()),
        machine_id: Uuid::new_v4(),
        project_id: Uuid::new_v4(),
        session_id: None,
        agent_run_id: None,
        locator: None,
        locator_key: None,
        session_name: None,
        window_id: None,
        title: None,
        host_epoch: None,
        rows: Some(40),
        cols: Some(120),
        attempt_generation: 1,
        attempt_started_at: now,
        process: None,
        unresolved_writes: json!({}),
        automatic_write_quarantined_at: None,
        automatic_write_quarantine_action_key: None,
        created_at: now,
        updated_at: now,
        liveness_at: None,
    }
}

type Scripted = Result<Delivery, WriteFailure>;

/// One recorded dispatch: the row it went to, the step, and the durable
/// unresolved keys visible at the moment bytes would have been emitted.
#[derive(Debug, Clone)]
struct Call {
    terminal: Uuid,
    step: Step,
    latched_at_dispatch: Vec<String>,
}

#[derive(Default)]
struct FakeRuntime {
    calls: StdMutex<Vec<Call>>,
    script: StdMutex<VecDeque<Scripted>>,
    probe: StdMutex<Option<Arc<Pool>>>,
    /// Holds the next write after it is recorded: signals `entered`, then
    /// waits for `release`.
    gate: StdMutex<Option<Gate>>,
    batch: Option<FakeBatch>,
}

struct Gate {
    entered: oneshot::Sender<()>,
    release: oneshot::Receiver<()>,
}

/// One host request: each target's result id and operations.
type HostRequest = Vec<(String, Vec<BatchOp>)>;

#[derive(Default)]
struct FakeBatch {
    calls: StdMutex<Vec<HostRequest>>,
    script: StdMutex<VecDeque<Vec<(&'static str, Scripted)>>>,
}

impl FakeRuntime {
    fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    fn batching() -> Arc<Self> {
        Arc::new(Self {
            batch: Some(FakeBatch::default()),
            ..Self::default()
        })
    }

    fn probing(pool: &Arc<Pool>) -> Arc<Self> {
        let runtime = Self::default();
        *runtime.probe.lock().unwrap() = Some(pool.clone());
        Arc::new(runtime)
    }

    fn script(&self, outcomes: impl IntoIterator<Item = Scripted>) {
        self.script.lock().unwrap().extend(outcomes);
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }

    fn payloads(&self) -> Vec<String> {
        self.calls().into_iter().map(|c| c.step.payload).collect()
    }
}

impl Runtime for FakeRuntime {
    fn write<'a>(
        &'a self,
        terminal: &'a Terminal,
        step: &'a Step,
    ) -> BoxFuture<'a, Result<Delivery, WriteFailure>> {
        Box::pin(async move {
            let probe = self.probe.lock().unwrap().clone();
            let latched_at_dispatch = match probe {
                Some(pool) => pool
                    .transaction(None, async |tx| {
                        let row = repository::get(tx, terminal.id).await?.expect("row");
                        anyhow::Ok(
                            row.unresolved_writes
                                .as_object()
                                .expect("map")
                                .keys()
                                .cloned()
                                .collect(),
                        )
                    })
                    .await
                    .expect("probe read"),
                None => Vec::new(),
            };
            self.calls.lock().unwrap().push(Call {
                terminal: terminal.id,
                step: step.clone(),
                latched_at_dispatch,
            });
            let gate = self.gate.lock().unwrap().take();
            if let Some(Gate { entered, release }) = gate {
                entered.send(()).expect("test awaits entry");
                release.await.expect("gate released");
            }
            self.script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(Ok(Delivery::Delivered))
        })
    }

    fn batch(&self) -> Option<&dyn BatchRuntime> {
        self.batch.as_ref().map(|b| b as &dyn BatchRuntime)
    }
}

impl BatchRuntime for FakeBatch {
    fn write_batch(
        &self,
        targets: Vec<BatchTarget>,
    ) -> BoxFuture<'_, Result<Vec<BatchResult>, String>> {
        Box::pin(async move {
            self.calls.lock().unwrap().push(
                targets
                    .iter()
                    .map(|t| (t.result_id.clone(), t.operations.clone()))
                    .collect(),
            );
            let mut scripted = self.script.lock().unwrap().pop_front().unwrap_or_default();
            Ok(targets
                .into_iter()
                .map(|t| {
                    let outcome = scripted
                        .iter()
                        .position(|(id, _)| *id == t.result_id)
                        .map(|i| scripted.remove(i).1)
                        .unwrap_or(Ok(Delivery::Delivered));
                    BatchResult {
                        result_id: t.result_id,
                        outcome,
                    }
                })
                .collect())
        })
    }
}

fn runtimes(native: &Arc<FakeRuntime>, tmux: &Arc<FakeRuntime>) -> Runtimes {
    Runtimes {
        native: Some(native.clone() as Arc<dyn Runtime>),
        tmux: Some(tmux.clone() as Arc<dyn Runtime>),
    }
}

fn text(payload: &str) -> Step {
    Step {
        kind: Kind::Text,
        payload: payload.into(),
        submit: false,
    }
}

fn key(payload: &str) -> Step {
    Step {
        kind: Kind::Key,
        payload: payload.into(),
        submit: false,
    }
}

fn automatic(terminal: Uuid, action: &str, step: Step) -> WriteRequest {
    WriteRequest {
        terminal_id: terminal,
        action_key: action.into(),
        origin: Origin::Automatic,
        step,
        lease: None,
        idempotency_key: None,
    }
}

fn operator(terminal: Uuid, action: &str, step: Step, lease: Option<LeaseClaim>) -> WriteRequest {
    WriteRequest {
        origin: Origin::Operator,
        lease,
        ..automatic(terminal, action, step)
    }
}

fn failure(stage: Stage, code: &str) -> WriteFailure {
    WriteFailure {
        stage,
        code: code.into(),
        detail: format!("{code} detail"),
    }
}

fn uncertain(detail: &str) -> Delivery {
    Delivery::Uncertain {
        detail: detail.into(),
    }
}

async fn controller(leases: &Registry, terminal: Uuid) -> anyhow::Result<LeaseClaim> {
    let attachment = leases
        .attach(
            terminal,
            Uuid::new_v4(),
            Viewer::Web,
            Backend::Native,
            leases::Delivery::Proxy,
        )
        .await?;
    let control = leases.take_control(attachment, true).await?;
    anyhow::ensure!(control.granted, "control granted");
    Ok(LeaseClaim {
        attachment_id: attachment.id,
        generation: control.generation,
    })
}

#[tokio::test]
async fn runtime_is_resolved_from_terminal_row() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let native_row = hub.insert(Backend::Native).await?;
    let tmux_row = hub.insert(Backend::Tmux).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::new();
    let tmux = FakeRuntime::new();
    let coordinator = Coordinator::new(hub.pool.clone(), leases.clone(), runtimes(&native, &tmux));

    let outcome = coordinator
        .write(automatic(native_row, "a:native", text("to-native")))
        .await?;
    assert_eq!(outcome, Outcome::Delivered);
    let outcome = coordinator
        .write(automatic(tmux_row, "a:tmux", text("to-tmux")))
        .await?;
    assert_eq!(outcome, Outcome::Delivered);
    assert_eq!(native.payloads(), vec!["to-native"]);
    assert_eq!(tmux.payloads(), vec!["to-tmux"]);
    assert_eq!(native.calls()[0].terminal, native_row);
    assert_eq!(tmux.calls()[0].terminal, tmux_row);

    // A row whose backend has no runtime fails closed: no other runtime is a
    // fallback, nothing is emitted, and the stage-none refusal leaves no latch.
    let native_only = Coordinator::new(
        hub.pool.clone(),
        leases.clone(),
        Runtimes {
            native: Some(native.clone() as Arc<dyn Runtime>),
            tmux: None,
        },
    );
    let refused = native_only
        .write(automatic(tmux_row, "a:unavailable", text("lost")))
        .await;
    assert!(
        matches!(refused, Err(Refusal::RuntimeUnavailable(Backend::Tmux))),
        "{refused:?}"
    );
    assert_eq!(native.payloads(), vec!["to-native"]);
    assert_eq!(tmux.payloads(), vec!["to-tmux"]);
    assert!(hub.unresolved(tmux_row).await?.is_empty());

    // The row, not the request, names the backend; the database rejects any
    // backend the coordinator has no variant for.
    let unknown = hub
        .pool
        .transaction(None, async |tx| {
            tx.execute(
                "UPDATE terminals SET backend='screen' WHERE id=$1::text::uuid",
                &[&native_row.to_string()],
            )
            .await
        })
        .await;
    assert!(unknown.is_err(), "unknown backend must not persist");

    let missing = Uuid::new_v4();
    let refused = coordinator
        .write(automatic(missing, "a:missing", text("nowhere")))
        .await;
    assert!(
        matches!(refused, Err(Refusal::TerminalNotFound(id)) if id == missing),
        "{refused:?}"
    );
    assert_eq!(native.calls().len() + tmux.calls().len(), 2);
    Ok(())
}

#[tokio::test]
async fn stale_lease_never_emits_bytes() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = hub.insert(Backend::Native).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::new();
    let tmux = FakeRuntime::new();
    let coordinator = Arc::new(Coordinator::new(
        hub.pool.clone(),
        leases.clone(),
        runtimes(&native, &tmux),
    ));

    let first = controller(&leases, row).await?;
    let outcome = coordinator
        .write(operator(row, "op:1", text("held"), Some(first)))
        .await?;
    assert_eq!(outcome, Outcome::Delivered);

    // An operator write without a claim, or with a superseded one, is refused
    // before any bytes or latch exist.
    let refused = coordinator
        .write(operator(row, "op:none", text("unclaimed"), None))
        .await;
    assert!(matches!(refused, Err(Refusal::StaleLease)), "{refused:?}");
    let second = controller(&leases, row).await?;
    assert!(second.generation > first.generation);
    let mut keyed = operator(row, "op:stale", text("stale"), Some(first));
    keyed.idempotency_key = Some("idem-stale".into());
    let refused = coordinator.write(keyed).await;
    assert!(matches!(refused, Err(Refusal::StaleLease)), "{refused:?}");
    let refused = coordinator
        .write(operator(
            row,
            "op:wrong-generation",
            text("stale"),
            Some(LeaseClaim {
                attachment_id: second.attachment_id,
                generation: first.generation,
            }),
        ))
        .await;
    assert!(matches!(refused, Err(Refusal::StaleLease)), "{refused:?}");
    assert_eq!(native.payloads(), vec!["held"]);
    assert!(hub.unresolved(row).await?.is_empty());

    // Serialization: a takeover waits for the in-flight operator write, and a
    // write queued behind the takeover revalidates after it acquires the lock.
    let (entered_tx, entered) = oneshot::channel();
    let (release, release_rx) = oneshot::channel();
    *native.gate.lock().unwrap() = Some(Gate {
        entered: entered_tx,
        release: release_rx,
    });
    let in_flight = tokio::spawn({
        let coordinator = coordinator.clone();
        async move {
            coordinator
                .write(operator(row, "op:slow", text("slow"), Some(second)))
                .await
        }
    });
    entered.await?;
    let takeover = tokio::spawn({
        let leases = leases.clone();
        async move { controller(&leases, row).await }
    });
    // The test runtime is single-threaded and neither attach nor the terminal
    // lock does I/O, so one yield parks the takeover on that lock.
    tokio::task::yield_now().await;
    assert!(!takeover.is_finished(), "takeover ran during a held write");
    let queued = tokio::spawn({
        let coordinator = coordinator.clone();
        async move {
            coordinator
                .write(operator(row, "op:queued", text("queued"), Some(second)))
                .await
        }
    });
    tokio::task::yield_now().await;
    assert!(!queued.is_finished());
    assert_eq!(native.payloads(), vec!["held", "slow"]);
    release.send(()).expect("release gate");
    assert_eq!(in_flight.await??, Outcome::Delivered);
    let third = takeover.await??;
    assert_eq!(third.generation, second.generation + 1);
    let refused = queued.await?;
    assert!(matches!(refused, Err(Refusal::StaleLease)), "{refused:?}");
    assert_eq!(native.payloads(), vec!["held", "slow"]);

    let outcome = coordinator
        .write(operator(row, "op:current", text("current"), Some(third)))
        .await?;
    assert_eq!(outcome, Outcome::Delivered);
    assert_eq!(native.payloads(), vec!["held", "slow", "current"]);
    Ok(())
}

#[tokio::test]
async fn idempotency_and_uncertainty_are_fail_closed() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = hub.insert(Backend::Native).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::probing(&hub.pool);
    let tmux = FakeRuntime::new();
    let coordinator = Coordinator::new(hub.pool.clone(), leases.clone(), runtimes(&native, &tmux));
    let payload = "h\u{e9}llo \u{2603}\u{1b}[A\n";
    let keyed = |payload: &str| WriteRequest {
        idempotency_key: Some("idem-1".into()),
        step: Step {
            kind: Kind::Text,
            payload: payload.into(),
            submit: true,
        },
        ..automatic(row, "send:1", text(""))
    };

    native.script([Ok(uncertain("reply timed out"))]);
    let outcome = coordinator.write(keyed(payload)).await?;
    assert_eq!(
        outcome,
        Outcome::Uncertain {
            detail: "reply timed out".into()
        }
    );
    let calls = native.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0].latched_at_dispatch, vec!["send:1"]);
    let entry = hub.get(row).await?.unresolved_writes["send:1"].clone();
    assert_eq!(entry["origin"], "automatic");
    assert_eq!(entry["daemon_epoch"], leases.epoch().to_string());
    // Byte-identical to Python's _payload_fingerprint for the same request.
    assert_eq!(
        entry["payload_fingerprint"],
        "44d50acc661adcb0a618c3024416ecc4cd14f13ee732ecf735bdf8ab0d20483d"
    );

    // Same key and payload replays the recorded uncertain outcome.
    let replay = coordinator.write(keyed(payload)).await?;
    assert!(matches!(replay, Outcome::Uncertain { .. }), "{replay:?}");
    // Same key with a different payload conflicts.
    let conflict = coordinator.write(keyed("other")).await;
    assert!(
        matches!(conflict, Err(Refusal::IdempotencyConflict)),
        "{conflict:?}"
    );
    assert_eq!(native.calls().len(), 1);

    // A daemon restart is a new lease epoch and coordinator over the same
    // rows: the evidence still decides, so nothing is retried.
    let (restarted, _events) = Registry::new(16);
    let restarted = Arc::new(restarted);
    assert_ne!(restarted.epoch(), leases.epoch());
    let after_restart = Coordinator::new(hub.pool.clone(), restarted, runtimes(&native, &tmux));
    let replay = after_restart.write(keyed(payload)).await?;
    assert!(matches!(replay, Outcome::Uncertain { .. }), "{replay:?}");
    let suppressed = after_restart
        .write(automatic(row, "send:1", text("again")))
        .await?;
    assert_eq!(suppressed, Outcome::Suppressed);
    assert_eq!(native.calls().len(), 1);

    // A partial refusal may have landed bytes: the latch stays and suppresses.
    native.script([Err(failure(Stage::Partial, "host_write_partial"))]);
    let refused = coordinator
        .write(automatic(row, "send:2", text("half")))
        .await;
    assert!(
        matches!(&refused, Err(Refusal::Write(f)) if f.stage == Stage::Partial),
        "{refused:?}"
    );
    assert_eq!(
        coordinator
            .write(automatic(row, "send:2", text("half")))
            .await?,
        Outcome::Suppressed
    );

    // A stage-none refusal proves nothing landed: the latch is released and a
    // later attempt dispatches.
    native.script([Err(failure(Stage::None, "host_refused"))]);
    let refused = coordinator
        .write(automatic(row, "send:3", text("refused")))
        .await;
    assert!(
        matches!(&refused, Err(Refusal::Write(f)) if f.stage == Stage::None),
        "{refused:?}"
    );
    let mut keys = hub.unresolved(row).await?;
    keys.sort();
    assert_eq!(keys, vec!["send:1", "send:2"]);
    assert_eq!(
        coordinator
            .write(automatic(row, "send:3", text("retry")))
            .await?,
        Outcome::Delivered
    );
    assert_eq!(native.payloads().last().map(String::as_str), Some("retry"));
    Ok(())
}

#[tokio::test]
async fn each_evidence_mutation_path_is_explicit() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = hub.insert(Backend::Native).await?;
    let other_row = hub.insert(Backend::Native).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::probing(&hub.pool);
    let tmux = FakeRuntime::new();
    let coordinator = Coordinator::new(hub.pool.clone(), leases.clone(), runtimes(&native, &tmux));

    // Persist: unrelated evidence from an earlier operation.
    coordinator
        .retain_unresolved(row, "other", Origin::Automatic)
        .await?;
    coordinator
        .retain_unresolved(other_row, "foreign", Origin::Automatic)
        .await?;

    // Clear: a confirmed write is latched before dispatch and clears only its key.
    let outcome = coordinator
        .write(automatic(row, "confirmed", text("ok")))
        .await?;
    assert_eq!(outcome, Outcome::Delivered);
    let mut at_dispatch = native.calls()[0].latched_at_dispatch.clone();
    at_dispatch.sort();
    assert_eq!(at_dispatch, vec!["confirmed", "other"]);
    assert_eq!(hub.unresolved(row).await?, vec!["other"]);

    // Replace: a new uncertain write writes its own evidence; re-persisting the
    // same key replaces that entry in place and leaves the rest.
    native.script([Ok(uncertain("lost reply"))]);
    coordinator
        .write(automatic(row, "uncertain", text("maybe")))
        .await?;
    let before = hub.get(row).await?.unresolved_writes["uncertain"].clone();
    coordinator
        .retain_unresolved(row, "uncertain", Origin::Daemon)
        .await?;
    let after = hub.get(row).await?.unresolved_writes;
    let mut keys: Vec<_> = after.as_object().expect("map").keys().cloned().collect();
    keys.sort();
    assert_eq!(keys, vec!["other", "uncertain"]);
    assert_eq!(after["uncertain"]["origin"], "daemon");
    assert_eq!(before["origin"], "automatic");
    assert_eq!(after["other"]["origin"], "automatic");

    // Quarantine: automatic writes under any other key are declined untouched.
    coordinator.quarantine(row, "quarantined").await?;
    assert_eq!(
        hub.quarantine_key(row).await?.as_deref(),
        Some("quarantined")
    );
    let calls = native.calls().len();
    assert_eq!(
        coordinator
            .write(automatic(row, "blocked", text("x")))
            .await?,
        Outcome::Quarantined
    );
    assert_eq!(native.calls().len(), calls);

    // Operator release: host-observed input lifts the quarantine and nothing else.
    coordinator.observe_operator_input(row).await?;
    assert_eq!(hub.quarantine_key(row).await?, None);
    let mut keys = hub.unresolved(row).await?;
    keys.sort();
    assert_eq!(keys, vec!["other", "uncertain"]);

    // A delivered operator write releases a quarantine the same way.
    coordinator.quarantine(row, "quarantined").await?;
    let claim = controller(&leases, row).await?;
    coordinator
        .write(operator(row, "op", text("typed"), Some(claim)))
        .await?;
    assert_eq!(hub.quarantine_key(row).await?, None);
    let mut keys = hub.unresolved(row).await?;
    keys.sort();
    assert_eq!(keys, vec!["other", "uncertain"]);

    // Positive observation clears only its key, and the quarantine only when
    // that quarantine names the same key.
    coordinator.quarantine(row, "other").await?;
    coordinator.observe_resolved(row, "uncertain").await?;
    assert_eq!(hub.unresolved(row).await?, vec!["other"]);
    assert_eq!(hub.quarantine_key(row).await?.as_deref(), Some("other"));
    coordinator.observe_resolved(row, "other").await?;
    assert!(hub.unresolved(row).await?.is_empty());
    assert_eq!(hub.quarantine_key(row).await?, None);

    // Capacity bounds refuse before any bytes.
    let calls = native.calls().len();
    let refused = coordinator
        .write(automatic(row, &"k".repeat(257), text("x")))
        .await;
    assert!(matches!(refused, Err(Refusal::Capacity)), "{refused:?}");
    for i in 0..32 {
        coordinator
            .retain_unresolved(row, &format!("fill:{i}"), Origin::Automatic)
            .await?;
    }
    let refused = coordinator
        .write(automatic(row, "fill:overflow", text("x")))
        .await;
    assert!(matches!(refused, Err(Refusal::Capacity)), "{refused:?}");
    assert_eq!(native.calls().len(), calls);

    // Exit: every terminal-local item goes, and no other terminal's.
    coordinator.quarantine(row, "fill:0").await?;
    coordinator.clear_on_exit(row).await?;
    assert!(hub.unresolved(row).await?.is_empty());
    assert_eq!(hub.quarantine_key(row).await?, None);
    assert_eq!(hub.unresolved(other_row).await?, vec!["foreign"]);
    Ok(())
}

#[tokio::test]
async fn batch_and_sequence_stop_at_first_uncertain_step() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = hub.insert(Backend::Native).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::batching();
    let tmux = FakeRuntime::new();
    let coordinator = Coordinator::new(hub.pool.clone(), leases.clone(), runtimes(&native, &tmux));
    let sequence = |action: &str, latch: bool| SequenceRequest {
        terminal_id: row,
        action_key: action.into(),
        origin: Origin::Automatic,
        lease: None,
        latch,
        steps: vec![
            SequenceStep::Write(text("a")),
            SequenceStep::Delay(Duration::from_millis(1)),
            SequenceStep::Write(key("enter")),
            SequenceStep::Write(text("z")),
        ],
    };

    // Uncertain at step 2: steps after it never run and the latch stays.
    native.script([Ok(Delivery::Delivered), Ok(uncertain("enter lost"))]);
    let report = coordinator
        .run_sequence(sequence("seq:uncertain", true))
        .await;
    assert_eq!(report.step, Some(2));
    assert!(
        matches!(&report.result, Ok(Outcome::Uncertain { detail }) if detail == "enter lost"),
        "{report:?}"
    );
    assert_eq!(native.payloads(), vec!["a", "enter"]);
    assert_eq!(hub.unresolved(row).await?, vec!["seq:uncertain"]);

    // A typed refusal after an accepted step keeps the latch and names the step.
    native.script([
        Ok(Delivery::Delivered),
        Err(failure(Stage::None, "invalid_key")),
    ]);
    let report = coordinator
        .run_sequence(sequence("seq:refused", true))
        .await;
    assert_eq!(report.step, Some(2));
    assert!(
        matches!(&report.result, Err(Refusal::Write(f)) if f.code == "invalid_key"),
        "{report:?}"
    );
    let mut keys = hub.unresolved(row).await?;
    keys.sort();
    assert_eq!(keys, vec!["seq:refused", "seq:uncertain"]);

    // A stage-none refusal before anything was accepted releases the latch.
    native.script([Err(failure(Stage::None, "host_refused"))]);
    let report = coordinator.run_sequence(sequence("seq:first", true)).await;
    assert_eq!(report.step, Some(0));
    assert!(
        matches!(&report.result, Err(Refusal::Write(_))),
        "{report:?}"
    );
    assert!(!hub.unresolved(row).await?.contains(&"seq:first".into()));

    // Every step accepted: delivered, latch cleared, no failing step.
    let before = native.calls().len();
    let report = coordinator.run_sequence(sequence("seq:ok", true)).await;
    assert_eq!(report.step, None);
    assert!(
        matches!(report.result, Ok(Outcome::Delivered)),
        "{report:?}"
    );
    assert_eq!(native.calls().len(), before + 3);
    assert!(!hub.unresolved(row).await?.contains(&"seq:ok".into()));

    // An unlatched sequence leaves no evidence even when uncertain.
    native.script([Ok(uncertain("drain lost"))]);
    let report = coordinator.run_sequence(sequence("seq:drain", false)).await;
    assert_eq!(report.step, Some(0));
    assert!(matches!(report.result, Ok(Outcome::Uncertain { .. })));
    assert!(!hub.unresolved(row).await?.contains(&"seq:drain".into()));

    // Quarantine and an existing latch decline before the first step.
    let before = native.calls().len();
    let report = coordinator
        .run_sequence(sequence("seq:uncertain", true))
        .await;
    assert_eq!((report.step, report.result?), (None, Outcome::Suppressed));
    coordinator.quarantine(row, "elsewhere").await?;
    let report = coordinator.run_sequence(sequence("seq:new", true)).await;
    assert_eq!((report.step, report.result?), (None, Outcome::Quarantined));
    assert_eq!(native.calls().len(), before);
    coordinator.clear_on_exit(row).await?;

    // Native wake batch.
    let delivered = hub.insert(Backend::Native).await?;
    let prelude_lost = hub.insert(Backend::Native).await?;
    let enter_refused = hub.insert(Backend::Native).await?;
    let duplicated = hub.insert(Backend::Native).await?;
    let quarantined = hub.insert(Backend::Native).await?;
    let tmux_row = hub.insert(Backend::Tmux).await?;
    let missing = Uuid::new_v4();
    coordinator.quarantine(quarantined, "other").await?;
    let wake = |id: &str, terminal: Uuid| WakeRequest {
        result_id: id.into(),
        terminal_id: terminal,
        clear_action_key: format!("wake-clear:{terminal}"),
        wake_action_key: format!("wake:{terminal}"),
        operations: vec![
            BatchOp {
                kind: Kind::Text,
                payload: format!("wake {id}"),
                delay_ms: 0,
            },
            BatchOp {
                kind: Kind::Key,
                payload: "enter".into(),
                delay_ms: 5,
            },
        ],
    };
    let batch = native.batch.as_ref().expect("batch runtime");
    batch.script.lock().unwrap().extend([
        vec![("prelude-lost", Ok(uncertain("prelude reply lost")))],
        vec![("enter-refused", Err(failure(Stage::None, "host_refused")))],
    ]);
    let results = coordinator
        .run_native_wake_batch(vec![
            wake("delivered", delivered),
            wake("prelude-lost", prelude_lost),
            wake("enter-refused", enter_refused),
            wake("dup-1", duplicated),
            wake("dup-2", duplicated),
            wake("quarantined", quarantined),
            wake("tmux", tmux_row),
            wake("missing", missing),
        ])
        .await?;
    let by_id = |id: &str| {
        results
            .iter()
            .find(|r| r.result_id == id)
            .unwrap_or_else(|| panic!("result {id}"))
    };
    assert_eq!(results.len(), 8);
    assert_eq!(
        results
            .iter()
            .map(|r| r.result_id.as_str())
            .collect::<Vec<_>>(),
        vec![
            "delivered",
            "prelude-lost",
            "enter-refused",
            "dup-1",
            "dup-2",
            "quarantined",
            "tmux",
            "missing"
        ]
    );
    let delivered_result = by_id("delivered");
    assert_eq!(delivered_result.step, WakeStep::Enter);
    assert_eq!(delivered_result.outcome, Ok(Outcome::Delivered));
    let lost = by_id("prelude-lost");
    assert_eq!(lost.step, WakeStep::Prelude);
    assert!(matches!(lost.outcome, Ok(Outcome::Uncertain { .. })));
    let refused = by_id("enter-refused");
    assert_eq!(refused.step, WakeStep::Enter);
    assert!(
        matches!(&refused.outcome, Err(f) if f.stage == Stage::Partial && f.code == "host_refused"),
        "{refused:?}"
    );
    for (id, code) in [
        ("dup-1", "duplicate_terminal"),
        ("dup-2", "duplicate_terminal"),
        ("tmux", "native_batch_unavailable"),
        ("missing", "terminal_not_found"),
    ] {
        let result = by_id(id);
        assert_eq!(result.step, WakeStep::Preflight, "{id}");
        assert!(
            matches!(&result.outcome, Err(f) if f.stage == Stage::None && f.code == code),
            "{id}: {result:?}"
        );
    }
    let quarantined_result = by_id("quarantined");
    assert_eq!(quarantined_result.step, WakeStep::Preflight);
    assert_eq!(quarantined_result.outcome, Ok(Outcome::Quarantined));

    // Phases: one prelude batch, then Enter only where the prelude was
    // delivered, with the pacing delay already spent.
    let calls = batch.calls.lock().unwrap().clone();
    assert_eq!(calls.len(), 2);
    let prelude: Vec<_> = calls[0]
        .iter()
        .map(|(id, ops)| (id.as_str(), ops.len()))
        .collect();
    assert_eq!(
        prelude,
        vec![("delivered", 1), ("prelude-lost", 1), ("enter-refused", 1)]
    );
    assert!(calls[0].iter().all(|(_, ops)| ops[0].kind == Kind::Text));
    let enter: Vec<_> = calls[1].iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(enter, vec!["delivered", "enter-refused"]);
    assert!(
        calls[1]
            .iter()
            .all(|(_, ops)| ops.len() == 1 && ops[0].payload == "enter" && ops[0].delay_ms == 0)
    );
    assert!(
        native.calls().len() == before,
        "batch never uses single writes"
    );
    assert!(tmux.calls().is_empty());

    // Settlement: delivered clears its wake latch; uncertain and partial keep it.
    assert!(hub.unresolved(delivered).await?.is_empty());
    assert_eq!(
        hub.unresolved(prelude_lost).await?,
        vec![format!("wake:{prelude_lost}")]
    );
    assert_eq!(
        hub.unresolved(enter_refused).await?,
        vec![format!("wake:{enter_refused}")]
    );
    assert!(hub.unresolved(duplicated).await?.is_empty());
    assert!(hub.unresolved(quarantined).await?.is_empty());
    Ok(())
}

#[tokio::test]
async fn unnamed_keys_are_refused_before_any_latch() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = hub.insert(Backend::Native).await?;
    let (leases, _events) = Registry::new(16);
    let leases = Arc::new(leases);
    let native = FakeRuntime::batching();
    let tmux = FakeRuntime::new();
    let coordinator = Coordinator::new(hub.pool.clone(), leases.clone(), runtimes(&native, &tmux));
    let invalid = |result: &Result<Outcome, Refusal>| {
        matches!(result, Err(Refusal::Write(WriteFailure { stage: Stage::None, code, .. }))
            if code == "invalid_key")
    };

    let refused = coordinator
        .write(automatic(row, "auto:raw", key("\u{1b}[2~")))
        .await;
    assert!(invalid(&refused), "{refused:?}");

    // A sequence is refused whole, naming the bad step, before its first write.
    let report = coordinator
        .run_sequence(SequenceRequest {
            terminal_id: row,
            action_key: "seq:raw".into(),
            origin: Origin::Automatic,
            lease: None,
            latch: true,
            steps: vec![
                SequenceStep::Write(text("a")),
                SequenceStep::Write(key("ctrl_z")),
            ],
        })
        .await;
    assert_eq!(report.step, Some(1));
    assert!(invalid(&report.result), "{report:?}");

    let results = coordinator
        .run_native_wake_batch(vec![WakeRequest {
            result_id: "raw".into(),
            terminal_id: row,
            clear_action_key: "wake-clear:raw".into(),
            wake_action_key: "wake:raw".into(),
            operations: vec![
                BatchOp {
                    kind: Kind::Text,
                    payload: "wake".into(),
                    delay_ms: 0,
                },
                BatchOp {
                    kind: Kind::Key,
                    payload: "f13".into(),
                    delay_ms: 0,
                },
            ],
        }])
        .await?;
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].step, WakeStep::Preflight);
    assert!(
        matches!(&results[0].outcome, Err(WriteFailure { stage: Stage::None, code, .. })
            if code == "invalid_key"),
        "{results:?}"
    );

    assert!(native.calls().is_empty());
    let batch = native.batch.as_ref().expect("batch runtime");
    assert!(batch.calls.lock().unwrap().is_empty());
    assert!(hub.unresolved(row).await?.is_empty());
    Ok(())
}
