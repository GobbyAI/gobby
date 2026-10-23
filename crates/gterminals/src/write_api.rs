//! The loopback write route's admission: the current backend child's token.

use std::collections::HashMap;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, RwLock};
use std::time::Duration;

use base64::{Engine, engine::general_purpose::STANDARD};
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::oneshot;
use tokio::time::Instant;
use uuid::Uuid;

use crate::control::MAX_CONTROL_LINE;
use crate::hold::{ActionGate, EffectId, HoldError, HoldToken, UncertainCause};
use crate::write::{
    Coordinator, Kind, LeaseClaim, Origin, Outcome, Refusal, SequenceReport, SequenceRequest,
    SequenceStep, Stage, Step, WriteRequest,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum WriteRefused {
    #[error("no supervised backend is attached")]
    NoBackend,
    #[error("the request carries no write token")]
    MissingToken,
    #[error("the write token is not the current backend's")]
    WrongToken,
}

/// The token gdaemon minted for the backend child it supervises now.
#[derive(Clone, Default)]
pub struct TokenSlot {
    current: Arc<RwLock<Option<Arc<str>>>>,
}

/// Holds one start's token in its slot; dropping it retires that token only.
pub struct TokenLease {
    slot: TokenSlot,
    token: Arc<str>,
}

impl TokenSlot {
    pub fn install(&self, token: String) -> TokenLease {
        let token: Arc<str> = token.into();
        *self.current.write().unwrap_or_else(PoisonError::into_inner) = Some(token.clone());
        TokenLease {
            slot: self.clone(),
            token,
        }
    }

    /// The token now in force, or `None` when no backend child is attached.
    pub fn current(&self) -> Option<Arc<str>> {
        self.current
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    pub fn authorize(&self, presented: Option<&str>) -> Result<(), WriteRefused> {
        let current = self.current.read().unwrap_or_else(PoisonError::into_inner);
        let current = current.as_deref().ok_or(WriteRefused::NoBackend)?;
        match presented {
            None | Some("") => Err(WriteRefused::MissingToken),
            Some(presented) if constant_time_eq(presented.as_bytes(), current.as_bytes()) => Ok(()),
            Some(_) => Err(WriteRefused::WrongToken),
        }
    }
}

impl Drop for TokenLease {
    fn drop(&mut self) {
        let mut current = self
            .slot
            .current
            .write()
            .unwrap_or_else(PoisonError::into_inner);
        if current
            .as_ref()
            .is_some_and(|c| Arc::ptr_eq(c, &self.token))
        {
            *current = None;
        }
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .zip(right)
            .fold(0_u8, |acc, (a, b)| acc | (a ^ b))
            == 0
}

/// How often a caller waiting on a queued reply re-pumps its terminal.
const ADMIT_POLL: Duration = Duration::from_secs(1);
/// Largest decoded paste one route call admits.
pub const MAX_PASTE_BYTES: usize = 1024 * 1024;
/// Largest decoded input one route call admits.
pub const MAX_INPUT_BYTES: usize = 64 * 1024;
/// Largest body the route reads: one control line plus its envelope.
pub const MAX_ROUTE_BODY: usize = MAX_CONTROL_LINE + 64 * 1024;

/// One route call, as the JSON envelope names it.
#[derive(Deserialize)]
#[serde(tag = "call", rename_all = "snake_case")]
enum Call {
    Write {
        terminal_id: Uuid,
        action_key: String,
        origin: Origin,
        step: WireStep,
        #[serde(default)]
        lease: Option<LeaseClaim>,
        #[serde(default)]
        hold: Option<WireHold>,
        #[serde(default)]
        idempotency_key: Option<String>,
    },
    RunSequence {
        terminal_id: Uuid,
        action_key: String,
        origin: Origin,
        #[serde(default)]
        lease: Option<LeaseClaim>,
        latch: bool,
        steps: Vec<WireSequenceStep>,
    },
    AcquireAction {
        terminal_id: Uuid,
        lease: LeaseClaim,
    },
    ReleaseAction {
        hold: WireHold,
    },
    RetainUnresolved {
        terminal_id: Uuid,
        action_key: String,
        origin: Origin,
    },
    Quarantine {
        terminal_id: Uuid,
        action_key: String,
    },
}

#[derive(Deserialize)]
struct WireStep {
    kind: Kind,
    /// Base64 of the bytes to write.
    payload: String,
    submit: bool,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum WireSequenceStep {
    Write { write: WireStep },
    Delay { delay_ms: u64 },
}

#[derive(Clone, Copy, Deserialize)]
struct WireHold {
    id: Uuid,
}

/// The loopback write route over the coordinator. Each terminal admits one
/// held action or one admitted effect at a time; other writes queue behind it.
pub struct Route {
    token: TokenSlot,
    shared: Arc<Shared>,
}

struct Shared {
    coordinator: Arc<Coordinator>,
    gates: Mutex<HashMap<Uuid, ActionGate<Pending>>>,
    holds: Mutex<HashMap<Uuid, HeldAction>>,
    active: AtomicUsize,
}

struct Pending {
    work: Work,
    reply: oneshot::Sender<Value>,
}

enum Work {
    Write(WriteRequest),
    Sequence(SequenceRequest),
}

/// What an acquired hold is bound to: its lease, the backend token in force
/// and the row's generation when the hold was taken.
#[derive(Clone)]
struct HeldAction {
    terminal_id: Uuid,
    lease: LeaseClaim,
    token: Arc<str>,
    attempt_generation: i32,
    host_epoch: Option<String>,
    hold: HoldToken,
}

impl Route {
    pub fn new(coordinator: Arc<Coordinator>, token: TokenSlot) -> Self {
        Self {
            token,
            shared: Arc::new(Shared {
                coordinator,
                gates: Mutex::new(HashMap::new()),
                holds: Mutex::new(HashMap::new()),
                active: AtomicUsize::new(0),
            }),
        }
    }

    /// Admitted effects that have not settled.
    pub fn active(&self) -> usize {
        self.shared.active.load(Ordering::SeqCst)
    }

    pub async fn handle(&self, call: Value) -> Value {
        let Ok(call) = serde_json::from_value::<Call>(call) else {
            return refused(
                Stage::None,
                "malformed_call",
                "the call does not match the route envelope",
            );
        };
        match call {
            Call::Write {
                terminal_id,
                action_key,
                origin,
                step,
                lease,
                hold,
                idempotency_key,
            } => {
                let step = match admit_step(step) {
                    Ok(step) => step,
                    Err(reply) => return reply,
                };
                let bytes = step.payload.len();
                let request = WriteRequest {
                    terminal_id,
                    action_key,
                    origin,
                    step,
                    lease,
                    idempotency_key,
                };
                match hold {
                    Some(hold) => self.held_write(hold.id, request).await,
                    None => self.enqueue(terminal_id, Work::Write(request), bytes).await,
                }
            }
            Call::RunSequence {
                terminal_id,
                action_key,
                origin,
                lease,
                latch,
                steps,
            } => {
                let (steps, bytes) = match admit_sequence(steps) {
                    Ok(admitted) => admitted,
                    Err((index, reply)) => return json!({"failed_step": index, "outcome": reply}),
                };
                let request = SequenceRequest {
                    terminal_id,
                    action_key,
                    origin,
                    lease,
                    latch,
                    steps,
                };
                self.enqueue(terminal_id, Work::Sequence(request), bytes)
                    .await
            }
            Call::AcquireAction { terminal_id, lease } => self.acquire(terminal_id, lease).await,
            Call::ReleaseAction { hold } => self.release(hold.id).await,
            Call::RetainUnresolved {
                terminal_id,
                action_key,
                origin,
            } => settled(
                self.shared
                    .coordinator
                    .retain_unresolved(terminal_id, &action_key, origin)
                    .await,
            ),
            Call::Quarantine {
                terminal_id,
                action_key,
            } => settled(
                self.shared
                    .coordinator
                    .quarantine(terminal_id, &action_key)
                    .await,
            ),
        }
    }

    /// Queues an unheld operation and waits for its reply.
    async fn enqueue(&self, terminal: Uuid, work: Work, bytes: usize) -> Value {
        let (reply, receiver) = oneshot::channel();
        let submitted = {
            let mut gates = lock(&self.shared.gates);
            let gate = gates.entry(terminal).or_default();
            // An idle gate admits at once, so its operation holds no queue bytes.
            let counted = if gate.is_idle() { 0 } else { bytes };
            gate.submit(Pending { work, reply }, counted, Instant::now())
        };
        if let Err(error) = submitted {
            return hold_refusal(error);
        }
        pump(&self.shared, terminal);
        receive(&self.shared, terminal, receiver).await
    }

    /// Runs one step under the hold it names. The binding must still match the
    /// lease, the token in force and the row's attempt and host epoch.
    async fn held_write(&self, id: Uuid, request: WriteRequest) -> Value {
        let held = lock(&self.shared.holds).get(&id).cloned();
        let Some(held) = held else {
            return stale_hold();
        };
        if held.terminal_id != request.terminal_id
            || request.lease != Some(held.lease)
            || !self.token_is(&held.token)
        {
            return stale_hold();
        }
        let terminal = request.terminal_id;
        let row = match self.shared.coordinator.require(terminal).await {
            Ok(row) => row,
            Err(error) => return encode_refusal(&error),
        };
        if row.attempt_generation != held.attempt_generation || row.host_epoch != held.host_epoch {
            return stale_hold();
        }
        let begun = lock(&self.shared.gates)
            .entry(terminal)
            .or_default()
            .begin(held.hold, Instant::now());
        match begun {
            Ok(effect) => {
                let (reply, receiver) = oneshot::channel();
                let pending = Pending {
                    work: Work::Write(request),
                    reply,
                };
                spawn_execute(&self.shared, terminal, effect, pending);
                receive(&self.shared, terminal, receiver).await
            }
            Err(HoldError::SupersededToken) => {
                lock(&self.shared.holds).remove(&id);
                stale_hold()
            }
            Err(error) => hold_refusal(error),
        }
    }

    async fn acquire(&self, terminal: Uuid, lease: LeaseClaim) -> Value {
        let coordinator = &self.shared.coordinator;
        if let Err(error) = coordinator
            .revalidate(terminal, Origin::Operator, Some(lease))
            .await
        {
            return encode_refusal(&error);
        }
        let row = match coordinator.require(terminal).await {
            Ok(row) => row,
            Err(error) => return encode_refusal(&error),
        };
        let Some(token) = self.token.current() else {
            return refused(Stage::None, "no_backend", "no backend child is attached");
        };
        let taken = lock(&self.shared.gates)
            .entry(terminal)
            .or_default()
            .acquire(Instant::now());
        let hold = match taken {
            Ok(hold) => hold,
            Err(error) => return hold_refusal(error),
        };
        let id = Uuid::new_v4();
        let held = HeldAction {
            terminal_id: terminal,
            lease,
            token,
            attempt_generation: row.attempt_generation,
            host_epoch: row.host_epoch,
            hold,
        };
        lock(&self.shared.holds).insert(id, held);
        json!({"status": "held", "hold": {"id": id}})
    }

    async fn release(&self, id: Uuid) -> Value {
        let removed = lock(&self.shared.holds).remove(&id);
        let Some(held) = removed else {
            return stale_hold();
        };
        let released = {
            let mut gates = lock(&self.shared.gates);
            gates
                .get_mut(&held.terminal_id)
                .map(|gate| gate.release(held.hold, Instant::now()))
        };
        pump(&self.shared, held.terminal_id);
        match released {
            Some(Ok(())) if self.token_is(&held.token) => json!({"status": "released"}),
            _ => stale_hold(),
        }
    }

    fn token_is(&self, token: &Arc<str>) -> bool {
        self.token
            .current()
            .is_some_and(|current| Arc::ptr_eq(&current, token))
    }
}

/// Admits the terminal's oldest queued operation when nothing blocks it.
fn pump(shared: &Arc<Shared>, terminal: Uuid) {
    let admitted = lock(&shared.gates)
        .get_mut(&terminal)
        .and_then(|gate| gate.next(Instant::now()));
    if let Some(admitted) = admitted {
        spawn_execute(shared, terminal, admitted.effect, admitted.op);
    }
}

/// Runs an admitted operation in its own task, so a caller that goes away
/// cannot cancel the write. The effect settles only after its reply is sent;
/// a reply that cannot be delivered leaves the effect uncertain.
fn spawn_execute(shared: &Arc<Shared>, terminal: Uuid, effect: EffectId, pending: Pending) {
    shared.active.fetch_add(1, Ordering::SeqCst);
    let shared = Arc::clone(shared);
    tokio::spawn(async move {
        let Pending { work, reply } = pending;
        let value = match work {
            Work::Write(request) => encode_write(shared.coordinator.write(request).await),
            Work::Sequence(request) => {
                encode_sequence(shared.coordinator.run_sequence(request).await)
            }
        };
        {
            let mut gates = lock(&shared.gates);
            if let Some(gate) = gates.get_mut(&terminal) {
                let settled = if reply.send(value).is_ok() {
                    gate.finish(effect, Instant::now())
                } else {
                    gate.uncertain(effect, UncertainCause::Disconnected)
                };
                debug_assert!(settled.is_ok(), "the admitted effect is in flight");
            }
        }
        pump(&shared, terminal);
        shared.active.fetch_sub(1, Ordering::SeqCst);
    });
}

/// Waits for the admitted reply. A lapsed hold sends no wake-up, so the wait
/// re-pumps the terminal's queue at each interval until the reply arrives.
async fn receive(
    shared: &Arc<Shared>,
    terminal: Uuid,
    mut receiver: oneshot::Receiver<Value>,
) -> Value {
    loop {
        match tokio::time::timeout(ADMIT_POLL, &mut receiver).await {
            Ok(reply) => {
                return reply.unwrap_or_else(|_| {
                    json!({"status": "uncertain", "detail": "the terminal action ended without a reply"})
                });
            }
            Err(_) => pump(shared, terminal),
        }
    }
}

fn admit_step(step: WireStep) -> Result<Step, Value> {
    if step.payload.len() > MAX_CONTROL_LINE {
        return Err(refused(
            Stage::None,
            "request_too_large",
            "the payload exceeds one control line",
        ));
    }
    let bytes = STANDARD.decode(&step.payload).map_err(|_| {
        refused(
            Stage::None,
            "undecodable_payload",
            "the payload is not base64",
        )
    })?;
    let limit = match step.kind {
        Kind::Paste => Some(MAX_PASTE_BYTES),
        Kind::Input => Some(MAX_INPUT_BYTES),
        Kind::Text | Kind::Key => None,
    };
    if limit.is_some_and(|limit| bytes.len() > limit) {
        return Err(refused(
            Stage::None,
            "request_too_large",
            "the decoded payload exceeds the operation's maximum",
        ));
    }
    let payload = String::from_utf8(bytes).map_err(|_| {
        refused(
            Stage::None,
            "undecodable_payload",
            "the payload is not UTF-8",
        )
    })?;
    Ok(Step {
        kind: step.kind,
        payload,
        submit: step.submit,
    })
}

/// Admits every step of a sequence. A refused step reports its index; no step
/// before it has run.
fn admit_sequence(
    steps: Vec<WireSequenceStep>,
) -> Result<(Vec<SequenceStep>, usize), (usize, Value)> {
    let mut admitted = Vec::with_capacity(steps.len());
    let mut bytes = 0;
    for (index, step) in steps.into_iter().enumerate() {
        match step {
            WireSequenceStep::Write { write } => {
                let step = admit_step(write).map_err(|reply| (index, reply))?;
                bytes += step.payload.len();
                admitted.push(SequenceStep::Write(step));
            }
            WireSequenceStep::Delay { delay_ms } => {
                admitted.push(SequenceStep::Delay(Duration::from_millis(delay_ms)));
            }
        }
    }
    Ok((admitted, bytes))
}

fn encode_write(result: Result<Outcome, Refusal>) -> Value {
    match result {
        Ok(Outcome::Delivered) => json!({"status": "delivered"}),
        Ok(Outcome::Uncertain { detail }) => json!({"status": "uncertain", "detail": detail}),
        Ok(Outcome::Suppressed) => json!({"status": "suppressed"}),
        Ok(Outcome::Quarantined) => json!({"status": "quarantined"}),
        Err(error) => encode_refusal(&error),
    }
}

fn encode_sequence(report: SequenceReport) -> Value {
    json!({"failed_step": report.step, "outcome": encode_write(report.result)})
}

fn settled(result: Result<(), Refusal>) -> Value {
    match result {
        Ok(()) => json!({"status": "ok"}),
        Err(error) => encode_refusal(&error),
    }
}

fn encode_refusal(error: &Refusal) -> Value {
    match error {
        Refusal::Write(failure) => json!({
            "status": "refusal",
            "stage": stage_name(failure.stage),
            "code": failure.code,
            "detail": failure.detail,
        }),
        Refusal::StaleLease => refused(Stage::None, "stale_lease", error.to_string()),
        Refusal::TerminalNotFound(_) => {
            refused(Stage::None, "terminal_not_found", error.to_string())
        }
        Refusal::RuntimeUnavailable(_) => {
            refused(Stage::None, "runtime_unavailable", error.to_string())
        }
        Refusal::IdempotencyConflict => {
            refused(Stage::None, "idempotency_conflict", error.to_string())
        }
        Refusal::Capacity => refused(Stage::None, "capacity", error.to_string()),
        Refusal::Repository(_) => refused(Stage::None, "repository", error.to_string()),
    }
}

fn hold_refusal(error: HoldError) -> Value {
    match error {
        HoldError::QueueFull { .. } => refused(Stage::None, "queue_full", error.to_string()),
        HoldError::QueueStale { .. } => refused(Stage::None, "queue_stale", error.to_string()),
        HoldError::Busy | HoldError::EffectInFlight => {
            refused(Stage::None, "hold_busy", error.to_string())
        }
        HoldError::SupersededToken | HoldError::StaleEffect => stale_hold(),
    }
}

fn stale_hold() -> Value {
    refused(Stage::None, "stale_hold", "the hold is no longer current")
}

fn refused(stage: Stage, code: &str, detail: impl Into<String>) -> Value {
    json!({
        "status": "refusal",
        "stage": stage_name(stage),
        "code": code,
        "detail": detail.into(),
    })
}

fn stage_name(stage: Stage) -> &'static str {
    match stage {
        Stage::None => "none",
        Stage::Partial => "partial",
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}
