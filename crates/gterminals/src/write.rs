//! Fail-closed terminal write coordinator.
//!
//! The port of `src/gobby/terminals/write_coordinator.py::WriteCoordinator`.
//! Every write runs under one per-terminal lock, re-reads the terminal row and
//! the lease after acquiring it, resolves its runtime from the row's backend,
//! and persists a write-ahead latch in `terminals.unresolved_writes` before any
//! byte is emitted. The latch is the only action-key store: a definite outcome
//! clears its own key, and an uncertain or partial one keeps it, so neither a
//! lost reply nor a daemon restart can replay a non-idempotent write.
//!
//! Dropping a call is its cancellation. A latch persisted before the drop stays
//! persisted, which fails closed: the action reads as unresolved until a
//! positive observation or terminal exit clears it.
//!
//! #22722's submit layer (composer verdicts and the held-submit retry records)
//! stays the single Python implementation above this coordinator.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fmt::Write as _;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use chrono::{SecondsFormat, Utc};
use gobby_core::postgres_pool::{Pool, TransactionError};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::OwnedMutexGuard;
use uuid::Uuid;

use crate::leases::Registry;
use crate::repository::{self, Backend, RepositoryError, Terminal};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Durable bounds shared with `gobby.storage.terminal_settlement`.
const ACTION_KEY_MAX_BYTES: usize = 256;
const UNRESOLVED_MAX_ENTRIES: usize = 32;
const UNRESOLVED_MAX_SERIALIZED_BYTES: usize = 65536;

/// `gobby.terminals.runtime.NamedKey`: the only payloads a `Kind::Key` step
/// may carry. The native host types any other key name as raw bytes.
const NAMED_KEYS: &[&str] = &[
    "enter",
    "escape",
    "tab",
    "backspace",
    "delete",
    "ctrl_c",
    "ctrl_k",
    "ctrl_l",
    "ctrl_u",
    "up",
    "down",
    "left",
    "right",
    "kp0",
    "kp1",
    "kp2",
    "kp3",
    "kp4",
    "kp5",
    "kp6",
    "kp7",
    "kp8",
    "kp9",
    "kpdecimal",
    "kpplus",
    "kpminus",
    "kpmul",
    "kpdiv",
    "kpenter",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Origin {
    Operator,
    Automatic,
    Attention,
    Daemon,
}

impl Origin {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Operator => "operator",
            Self::Automatic => "automatic",
            Self::Attention => "attention",
            Self::Daemon => "daemon",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Text,
    Key,
    Paste,
    Input,
}

impl Kind {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Key => "key",
            Self::Paste => "paste",
            Self::Input => "input",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Step {
    pub kind: Kind,
    pub payload: String,
    pub submit: bool,
}

/// The lease an operator write was admitted under.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LeaseClaim {
    pub attachment_id: Uuid,
    pub generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteRequest {
    pub terminal_id: Uuid,
    pub action_key: String,
    pub origin: Origin,
    pub step: Step,
    pub lease: Option<LeaseClaim>,
    pub idempotency_key: Option<String>,
}

/// What a runtime can report for bytes it attempted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Delivery {
    Delivered,
    Uncertain { detail: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Delivered,
    /// The bytes may or may not have landed; the latch stays persisted.
    Uncertain {
        detail: String,
    },
    /// An automatic action whose key is still unresolved.
    Suppressed,
    /// Automatic writes under other keys wait for operator input.
    Quarantined,
}

impl From<Delivery> for Outcome {
    fn from(delivery: Delivery) -> Self {
        match delivery {
            Delivery::Delivered => Self::Delivered,
            Delivery::Uncertain { detail } => Self::Uncertain { detail },
        }
    }
}

/// How far a refused write got: `None` proves no byte was emitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    None,
    Partial,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WriteFailure {
    pub stage: Stage,
    pub code: String,
    pub detail: String,
}

#[derive(Debug, thiserror::Error)]
pub enum Refusal {
    #[error("terminal {0} was not found")]
    TerminalNotFound(Uuid),
    #[error("lease is no longer current")]
    StaleLease,
    #[error("no runtime is registered for backend {0:?}")]
    RuntimeUnavailable(Backend),
    #[error("idempotency key was already used with a different payload")]
    IdempotencyConflict,
    #[error("unresolved_write_capacity")]
    Capacity,
    #[error("terminal write failed: {}", .0.detail)]
    Write(WriteFailure),
    #[error(transparent)]
    Repository(#[from] RepositoryError),
}

impl From<TransactionError> for Refusal {
    fn from(error: TransactionError) -> Self {
        Self::Repository(error.into())
    }
}

pub trait Runtime: Send + Sync {
    fn write<'a>(
        &'a self,
        terminal: &'a Terminal,
        step: &'a Step,
    ) -> BoxFuture<'a, Result<Delivery, WriteFailure>>;

    /// The native host's atomic multi-terminal write, when this runtime has one.
    fn batch(&self) -> Option<&dyn BatchRuntime> {
        None
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BatchOp {
    pub kind: Kind,
    pub payload: String,
    pub delay_ms: u64,
}

#[derive(Debug, Clone)]
pub struct BatchTarget {
    pub result_id: String,
    pub terminal: Terminal,
    pub operations: Vec<BatchOp>,
}

#[derive(Debug, Clone)]
pub struct BatchResult {
    pub result_id: String,
    pub outcome: Result<Delivery, WriteFailure>,
}

pub trait BatchRuntime: Send + Sync {
    /// One host request with a result per target; `Err` means the request's
    /// fate is unknown for every target.
    fn write_batch(
        &self,
        targets: Vec<BatchTarget>,
    ) -> BoxFuture<'_, Result<Vec<BatchResult>, String>>;
}

/// One runtime per backend. A row is dispatched only through the runtime its
/// own backend names; a missing one is refused, never substituted.
#[derive(Clone)]
pub struct Runtimes {
    pub native: Option<Arc<dyn Runtime>>,
    pub tmux: Option<Arc<dyn Runtime>>,
}

impl Runtimes {
    fn resolve(&self, backend: Backend) -> Result<&Arc<dyn Runtime>, Refusal> {
        match backend {
            Backend::Native => self.native.as_ref(),
            Backend::Tmux => self.tmux.as_ref(),
        }
        .ok_or(Refusal::RuntimeUnavailable(backend))
    }
}

#[derive(Debug, Clone)]
pub enum SequenceStep {
    Write(Step),
    Delay(Duration),
}

#[derive(Debug, Clone)]
pub struct SequenceRequest {
    pub terminal_id: Uuid,
    pub action_key: String,
    pub origin: Origin,
    pub lease: Option<LeaseClaim>,
    pub latch: bool,
    pub steps: Vec<SequenceStep>,
}

/// `step` is the index of the step that ended the sequence, or `None` when it
/// was declined before any step or every step was delivered.
#[derive(Debug)]
pub struct SequenceReport {
    pub step: Option<usize>,
    pub result: Result<Outcome, Refusal>,
}

type Stop = (Option<usize>, Result<Outcome, Refusal>);

#[derive(Debug, Clone)]
pub struct WakeRequest {
    pub result_id: String,
    pub terminal_id: Uuid,
    pub clear_action_key: String,
    pub wake_action_key: String,
    pub operations: Vec<BatchOp>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WakeStep {
    /// Declined before any byte was dispatched.
    Preflight,
    /// The text before the trailing Enter.
    Prelude,
    Enter,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WakeResult {
    pub result_id: String,
    pub step: WakeStep,
    pub outcome: Result<Outcome, WriteFailure>,
}

struct Prepared {
    index: usize,
    had_latch: bool,
    target: BatchTarget,
}

type Phased = (WakeStep, Result<Delivery, WriteFailure>);

pub struct Coordinator {
    pool: Arc<Pool>,
    leases: Arc<Registry>,
    runtimes: Runtimes,
}

impl Coordinator {
    pub fn new(pool: Arc<Pool>, leases: Arc<Registry>, runtimes: Runtimes) -> Self {
        Self {
            pool,
            leases,
            runtimes,
        }
    }

    /// One logical write. Clears its latch only on a delivered outcome or a
    /// refusal that proves no byte was emitted.
    pub async fn write(&self, request: WriteRequest) -> Result<Outcome, Refusal> {
        if let Some(failure) = invalid_key(request.step.kind, &request.step.payload) {
            return Err(Refusal::Write(failure));
        }
        let _held = self.acquire(request.terminal_id).await;
        let terminal = self.require(request.terminal_id).await?;
        let fingerprint = request
            .idempotency_key
            .as_ref()
            .map(|_| payload_fingerprint(&request.step));
        if let Some(fingerprint) = &fingerprint
            && let Some(entry) = terminal.unresolved_writes.get(&request.action_key)
        {
            if entry.get("payload_fingerprint").and_then(Value::as_str) != Some(fingerprint) {
                return Err(Refusal::IdempotencyConflict);
            }
            return Ok(Outcome::Uncertain {
                detail: "write outcome remains indeterminate".into(),
            });
        }
        if let Some(blocked) = blocked_automatic(&terminal, &request.action_key, request.origin) {
            return Ok(blocked);
        }
        self.revalidate(request.terminal_id, request.origin, request.lease)
            .await?;
        let runtime = self.runtimes.resolve(terminal.backend)?;
        // An operator write without an idempotency key has no latch reader: its
        // replay guard is the lease registry's write sequence, and a daemon
        // restart ends the attachment itself.
        let latch = request.origin != Origin::Operator || request.idempotency_key.is_some();
        if latch {
            self.persist(
                request.terminal_id,
                &request.action_key,
                request.origin,
                fingerprint.as_deref(),
            )
            .await?;
        }
        match runtime.write(&terminal, &request.step).await {
            Ok(Delivery::Delivered) => {
                if latch {
                    self.clear(request.terminal_id, &request.action_key).await?;
                }
                if request.origin == Origin::Operator
                    && terminal.automatic_write_quarantined_at.is_some()
                {
                    self.observe_operator_input(request.terminal_id).await?;
                }
                Ok(Outcome::Delivered)
            }
            Ok(Delivery::Uncertain { detail }) => Ok(Outcome::Uncertain { detail }),
            Err(failure) => {
                if latch && failure.stage == Stage::None {
                    self.clear(request.terminal_id, &request.action_key).await?;
                }
                Err(Refusal::Write(failure))
            }
        }
    }

    /// One logical action as ordered steps under the terminal lock. It stops
    /// at the first step that is not delivered and reports that step.
    ///
    /// `latch: false` skips the write-ahead latch for an action whose steps are
    /// idempotent on the terminal, such as a composer drain; quarantine still
    /// applies to it.
    pub async fn run_sequence(&self, request: SequenceRequest) -> SequenceReport {
        let _held = self.acquire(request.terminal_id).await;
        let (step, result) = match self.sequence_locked(&request).await {
            Ok(outcome) => (None, Ok(outcome)),
            Err(stop) => stop,
        };
        SequenceReport { step, result }
    }

    async fn sequence_locked(&self, request: &SequenceRequest) -> Result<Outcome, Stop> {
        let id = request.terminal_id;
        let key = request.action_key.as_str();
        let preflight = |error| (None, Err(error));
        for (index, step) in request.steps.iter().enumerate() {
            if let SequenceStep::Write(step) = step
                && let Some(failure) = invalid_key(step.kind, &step.payload)
            {
                return Err((Some(index), Err(Refusal::Write(failure))));
            }
        }
        let terminal = self.require(id).await.map_err(preflight)?;
        if let Some(blocked) = blocked_automatic(&terminal, key, request.origin) {
            return Ok(blocked);
        }
        let runtime = self.runtimes.resolve(terminal.backend).map_err(preflight)?;
        if request.latch {
            self.revalidate(id, request.origin, request.lease)
                .await
                .map_err(preflight)?;
            self.persist(id, key, request.origin, None)
                .await
                .map_err(preflight)?;
        }
        let mut dispatched = false;
        for (index, step) in request.steps.iter().enumerate() {
            let step = match step {
                SequenceStep::Delay(delay) => {
                    tokio::time::sleep(*delay).await;
                    continue;
                }
                SequenceStep::Write(step) => step,
            };
            if let Err(refusal) = self.revalidate(id, request.origin, request.lease).await {
                return Err(self.stop(id, key, !dispatched, index, refusal).await);
            }
            let result = runtime.write(&terminal, step).await;
            let earlier = dispatched;
            dispatched = true;
            match result {
                Ok(Delivery::Delivered) => {}
                Ok(Delivery::Uncertain { detail }) => {
                    return Err((Some(index), Ok(Outcome::Uncertain { detail })));
                }
                Err(failure) => {
                    let release = !earlier && failure.stage == Stage::None;
                    return Err(self
                        .stop(id, key, release, index, Refusal::Write(failure))
                        .await);
                }
            }
        }
        self.clear(id, key).await.map_err(preflight)?;
        Ok(Outcome::Delivered)
    }

    /// A refusal ends the sequence; its latch is released only when no step
    /// could have emitted bytes.
    async fn stop(
        &self,
        id: Uuid,
        key: &str,
        release: bool,
        index: usize,
        refusal: Refusal,
    ) -> Stop {
        if release && let Err(error) = self.clear(id, key).await {
            return (Some(index), Err(error));
        }
        (Some(index), Err(refusal))
    }

    /// Latches and orders one bounded native-host wake batch. Each target's
    /// trailing Enter goes in a second host request, only after its text was
    /// delivered, so a lost prelude never submits.
    pub async fn run_native_wake_batch(
        &self,
        requests: Vec<WakeRequest>,
    ) -> Result<Vec<WakeResult>, Refusal> {
        let mut seen = BTreeSet::new();
        let duplicates: BTreeSet<Uuid> = requests
            .iter()
            .filter(|r| !seen.insert(r.terminal_id))
            .map(|r| r.terminal_id)
            .collect();
        // Locks are taken in one global order so concurrent batches and single
        // writes cannot deadlock.
        let mut held = Vec::new();
        for id in seen.difference(&duplicates) {
            held.push(self.acquire(*id).await);
        }

        let mut results: Vec<Option<WakeResult>> = vec![None; requests.len()];
        let mut chosen: Option<&Arc<dyn Runtime>> = None;
        let mut prepared = Vec::new();
        for (index, request) in requests.iter().enumerate() {
            let refuse = |code: &str, detail: String| {
                Some(WakeResult {
                    result_id: request.result_id.clone(),
                    step: WakeStep::Preflight,
                    outcome: Err(WriteFailure {
                        stage: Stage::None,
                        code: code.into(),
                        detail,
                    }),
                })
            };
            let id = request.terminal_id;
            if duplicates.contains(&id) {
                results[index] = refuse(
                    "duplicate_terminal",
                    "native wake batch targets must be grouped by terminal".into(),
                );
                continue;
            }
            if let Some(failure) = request
                .operations
                .iter()
                .find_map(|op| invalid_key(op.kind, &op.payload))
            {
                results[index] = refuse(&failure.code, failure.detail);
                continue;
            }
            let Some(terminal) = self.load(id).await? else {
                results[index] =
                    refuse("terminal_not_found", format!("terminal {id} was not found"));
                continue;
            };
            if let Some(blocked) =
                blocked_automatic(&terminal, &request.clear_action_key, Origin::Automatic)
            {
                results[index] = Some(WakeResult {
                    result_id: request.result_id.clone(),
                    step: WakeStep::Preflight,
                    outcome: Ok(blocked),
                });
                continue;
            }
            let runtime = match self.runtimes.resolve(terminal.backend) {
                Ok(runtime) => runtime,
                Err(refusal) => {
                    results[index] = refuse("backend_unregistered", refusal.to_string());
                    continue;
                }
            };
            if runtime.batch().is_none() {
                results[index] = refuse(
                    "native_batch_unavailable",
                    "terminal runtime does not support native wake batching".into(),
                );
                continue;
            }
            match chosen {
                None => chosen = Some(runtime),
                Some(first) if !Arc::ptr_eq(first, runtime) => {
                    results[index] = refuse(
                        "native_batch_unavailable",
                        "native wake targets do not share one host connection".into(),
                    );
                    continue;
                }
                Some(_) => {}
            }
            let had_latch = terminal
                .unresolved_writes
                .get(&request.wake_action_key)
                .is_some();
            if !had_latch {
                match self
                    .persist(id, &request.wake_action_key, Origin::Automatic, None)
                    .await
                {
                    Ok(()) => {}
                    Err(Refusal::Capacity) => {
                        results[index] =
                            refuse("unresolved_write_capacity", Refusal::Capacity.to_string());
                        continue;
                    }
                    Err(error) => return Err(error),
                }
            }
            prepared.push(Prepared {
                index,
                had_latch,
                target: BatchTarget {
                    result_id: request.result_id.clone(),
                    terminal,
                    operations: request.operations.clone(),
                },
            });
        }

        if let Some(batch) = chosen.and_then(|runtime| runtime.batch()) {
            let dispatched = dispatch_wake(batch, &prepared).await;
            for (prepared, (step, outcome)) in prepared.iter().zip(dispatched) {
                let request = &requests[prepared.index];
                let settled = match &outcome {
                    Ok(Delivery::Delivered) => true,
                    Ok(Delivery::Uncertain { .. }) => false,
                    Err(failure) => failure.stage == Stage::None && !prepared.had_latch,
                };
                if settled {
                    self.clear(request.terminal_id, &request.wake_action_key)
                        .await?;
                }
                results[prepared.index] = Some(WakeResult {
                    result_id: request.result_id.clone(),
                    step,
                    outcome: outcome.map(Outcome::from),
                });
            }
        }
        drop(held);
        Ok(results.into_iter().flatten().collect())
    }

    /// Persist: keep an action latched, replacing only its own entry.
    pub async fn retain_unresolved(
        &self,
        terminal_id: Uuid,
        action_key: &str,
        origin: Origin,
    ) -> Result<(), Refusal> {
        self.persist(terminal_id, action_key, origin, None).await
    }

    /// Quarantine: decline automatic writes under every other action key.
    pub async fn quarantine(&self, terminal_id: Uuid, action_key: &str) -> Result<(), Refusal> {
        let found = self
            .pool
            .transaction(None, async |tx| {
                repository::set_write_quarantine(tx, terminal_id, action_key).await
            })
            .await?;
        found
            .then_some(())
            .ok_or(Refusal::TerminalNotFound(terminal_id))
    }

    /// Positive observation of one action: clears only that key, and the
    /// quarantine only when it names the same key.
    pub async fn observe_resolved(
        &self,
        terminal_id: Uuid,
        action_key: &str,
    ) -> Result<(), Refusal> {
        let found = self
            .pool
            .transaction(None, async |tx| {
                repository::resolve_unresolved_write(tx, terminal_id, action_key).await
            })
            .await?;
        found
            .then_some(())
            .ok_or(Refusal::TerminalNotFound(terminal_id))
    }

    /// Operator release: the host accepted direct input, so the automatic-write
    /// quarantine lifts. Unresolved writes are untouched.
    pub async fn observe_operator_input(&self, terminal_id: Uuid) -> Result<(), Refusal> {
        self.pool
            .transaction(None, async |tx| {
                repository::release_write_quarantine(tx, terminal_id).await
            })
            .await?;
        Ok(())
    }

    /// Exit cleanup: every unresolved write and the quarantine pair.
    pub async fn clear_on_exit(&self, terminal_id: Uuid) -> Result<(), Refusal> {
        let _held = self.acquire(terminal_id).await;
        let found = self
            .pool
            .transaction(None, async |tx| {
                repository::clear_write_evidence(tx, terminal_id).await
            })
            .await?;
        found
            .then_some(())
            .ok_or(Refusal::TerminalNotFound(terminal_id))
    }

    /// The registry's terminal lock, so a control transition waits for this
    /// write and a later write sees the transition.
    async fn acquire(&self, terminal_id: Uuid) -> OwnedMutexGuard<()> {
        self.leases.lock(terminal_id).await
    }

    async fn load(&self, terminal_id: Uuid) -> Result<Option<Terminal>, Refusal> {
        Ok(self
            .pool
            .transaction(None, async |tx| repository::get(tx, terminal_id).await)
            .await?)
    }

    async fn require(&self, terminal_id: Uuid) -> Result<Terminal, Refusal> {
        self.load(terminal_id)
            .await?
            .ok_or(Refusal::TerminalNotFound(terminal_id))
    }

    /// Operator writes must still hold the lease they were admitted under.
    async fn revalidate(
        &self,
        terminal_id: Uuid,
        origin: Origin,
        lease: Option<LeaseClaim>,
    ) -> Result<(), Refusal> {
        if origin != Origin::Operator {
            return Ok(());
        }
        let view = self.leases.snapshot(terminal_id).await;
        match lease {
            Some(claim)
                if view.holder == Some(claim.attachment_id)
                    && view.generation == claim.generation =>
            {
                Ok(())
            }
            _ => Err(Refusal::StaleLease),
        }
    }

    /// Write-ahead latch for one action key, inside the durable map bounds.
    async fn persist(
        &self,
        terminal_id: Uuid,
        action_key: &str,
        origin: Origin,
        fingerprint: Option<&str>,
    ) -> Result<(), Refusal> {
        if action_key.is_empty() || action_key.len() > ACTION_KEY_MAX_BYTES {
            return Err(Refusal::Capacity);
        }
        let mut entry = json!({
            "at": Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false),
            "origin": origin.as_str(),
            "daemon_epoch": self.leases.epoch().to_string(),
        });
        if let Some(fingerprint) = fingerprint {
            entry["payload_fingerprint"] = fingerprint.into();
        }
        self.pool
            .transaction(None, async |tx| {
                let terminal = repository::get_for_update(tx, terminal_id)
                    .await?
                    .ok_or(Refusal::TerminalNotFound(terminal_id))?;
                let mut writes = terminal
                    .unresolved_writes
                    .as_object()
                    .cloned()
                    .unwrap_or_default();
                if !writes.contains_key(action_key) && writes.len() >= UNRESOLVED_MAX_ENTRIES {
                    return Err(Refusal::Capacity);
                }
                writes.insert(action_key.into(), entry);
                let writes = Value::Object(writes);
                if ascii_json_len(&writes.to_string()) > UNRESOLVED_MAX_SERIALIZED_BYTES {
                    return Err(Refusal::Capacity);
                }
                repository::set_unresolved_writes(tx, terminal_id, &writes).await?;
                Ok(())
            })
            .await
    }

    /// Clear: drop exactly one key.
    async fn clear(&self, terminal_id: Uuid, action_key: &str) -> Result<(), Refusal> {
        let found = self
            .pool
            .transaction(None, async |tx| {
                repository::clear_unresolved_write(tx, terminal_id, action_key).await
            })
            .await?;
        found
            .then_some(())
            .ok_or(Refusal::TerminalNotFound(terminal_id))
    }
}

/// A key step must name a key; refused before any latch or byte.
fn invalid_key(kind: Kind, payload: &str) -> Option<WriteFailure> {
    (kind == Kind::Key && !NAMED_KEYS.contains(&payload)).then(|| WriteFailure {
        stage: Stage::None,
        code: "invalid_key".into(),
        detail: "key payload is not a named key".into(),
    })
}

fn blocked_automatic(terminal: &Terminal, action_key: &str, origin: Origin) -> Option<Outcome> {
    if origin != Origin::Automatic {
        return None;
    }
    if terminal.automatic_write_quarantined_at.is_some()
        && terminal.automatic_write_quarantine_action_key.as_deref() != Some(action_key)
    {
        return Some(Outcome::Quarantined);
    }
    terminal
        .unresolved_writes
        .get(action_key)
        .map(|_| Outcome::Suppressed)
}

/// Byte-identical to Python's `_payload_fingerprint`: SHA-256 of compact,
/// key-sorted JSON over kind, payload, and submit.
fn payload_fingerprint(step: &Step) -> String {
    let encoded = json!({
        "kind": step.kind.as_str(),
        "payload": step.payload,
        "submit": step.submit,
    })
    .to_string();
    Sha256::digest(encoded.as_bytes())
        .iter()
        .fold(String::with_capacity(64), |mut hex, byte| {
            let _ = write!(hex, "{byte:02x}");
            hex
        })
}

/// Length of `encoded` once Python's `json.dumps` escapes non-ASCII, which is
/// how the durable size bound is measured.
fn ascii_json_len(encoded: &str) -> usize {
    encoded
        .chars()
        .map(|c| match c as u32 {
            0..0x80 => 1,
            0x80..0x1_0000 => 6,
            _ => 12,
        })
        .sum()
}

/// Two host requests: every prelude, then the Enter of each target whose
/// prelude was delivered. A transport failure leaves every target uncertain.
async fn dispatch_wake(batch: &dyn BatchRuntime, prepared: &[Prepared]) -> Vec<Phased> {
    let mut prelude = Vec::new();
    let mut enter = Vec::new();
    let mut has_prelude = HashSet::new();
    for item in prepared {
        let target = &item.target;
        match target.operations.split_last() {
            Some((last, rest)) if last.kind == Kind::Key && last.payload == "enter" => {
                enter.push(BatchTarget {
                    operations: vec![last.clone()],
                    ..target.clone()
                });
                if !rest.is_empty() {
                    has_prelude.insert(target.result_id.clone());
                    prelude.push(BatchTarget {
                        operations: rest.to_vec(),
                        ..target.clone()
                    });
                }
            }
            _ => prelude.push(target.clone()),
        }
    }
    let mut by_id: HashMap<String, Phased> = HashMap::new();
    let mut entered = HashSet::new();
    // The step a target had reached when its outcome went unknown.
    let unknown = |entered: &HashSet<String>, item: &Prepared, detail: &str| -> Phased {
        let step = if entered.contains(&item.target.result_id) {
            WakeStep::Enter
        } else {
            WakeStep::Prelude
        };
        (
            step,
            Ok(Delivery::Uncertain {
                detail: detail.into(),
            }),
        )
    };
    let uncertain = |entered: &HashSet<String>, detail: &str| {
        prepared
            .iter()
            .map(|item| unknown(entered, item, detail))
            .collect()
    };
    if !prelude.is_empty() {
        match batch.write_batch(prelude).await {
            Ok(results) => {
                for result in results {
                    by_id.insert(result.result_id, (WakeStep::Prelude, result.outcome));
                }
            }
            Err(detail) => return uncertain(&entered, &detail),
        }
    }
    let mut ready: Vec<BatchTarget> = enter
        .into_iter()
        .filter(|target| {
            !has_prelude.contains(&target.result_id)
                || matches!(
                    by_id.get(&target.result_id),
                    Some((_, Ok(Delivery::Delivered)))
                )
        })
        .collect();
    if let Some(pace) = ready.iter().map(|t| t.operations[0].delay_ms).max() {
        // Pace before submitting, so a hook read during the text/Enter gap
        // cannot acknowledge the new prompt.
        tokio::time::sleep(Duration::from_millis(pace)).await;
        for target in &mut ready {
            target.operations[0].delay_ms = 0;
            entered.insert(target.result_id.clone());
        }
        match batch.write_batch(ready).await {
            Ok(results) => {
                for mut result in results {
                    // A refused Enter is still a partial wake once its text
                    // reached the composer, so the latch stays.
                    if has_prelude.contains(&result.result_id)
                        && let Err(failure) = &mut result.outcome
                        && failure.stage == Stage::None
                    {
                        failure.stage = Stage::Partial;
                    }
                    by_id.insert(result.result_id, (WakeStep::Enter, result.outcome));
                }
            }
            Err(detail) => return uncertain(&entered, &detail),
        }
    }
    prepared
        .iter()
        .map(|item| {
            by_id.remove(&item.target.result_id).unwrap_or_else(|| {
                unknown(
                    &entered,
                    item,
                    "native host returned no result for this target",
                )
            })
        })
        .collect()
}
