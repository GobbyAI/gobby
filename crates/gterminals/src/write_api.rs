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
use crate::hold::{ActionGate, EffectId, HoldError, HoldToken, QUEUE_MAX_AGE, UncertainCause};
use crate::write::{
    BatchOp, Coordinator, Kind, LeaseClaim, Origin, Outcome, Refusal, SequenceReport,
    SequenceRequest, SequenceStep, Stage, Step, WakeRequest, WakeResult, WakeStep, WriteRequest,
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

    /// Whether `token` is still the token in force. A retired token never is.
    pub fn is_current(&self, token: &Arc<str>) -> bool {
        self.current
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, token))
    }

    /// Admits a presented token, returning the in-force token it matched.
    pub fn admit(&self, presented: Option<&str>) -> Result<Arc<str>, WriteRefused> {
        let current = self.current.read().unwrap_or_else(PoisonError::into_inner);
        let current = current.as_ref().ok_or(WriteRefused::NoBackend)?;
        match presented {
            None | Some("") => Err(WriteRefused::MissingToken),
            Some(presented) if constant_time_eq(presented.as_bytes(), current.as_bytes()) => {
                Ok(Arc::clone(current))
            }
            Some(_) => Err(WriteRefused::WrongToken),
        }
    }

    pub fn authorize(&self, presented: Option<&str>) -> Result<(), WriteRefused> {
        self.admit(presented).map(|_| ())
    }
}

/// The token an admitted call was authorized under. The coordinator checks it
/// immediately before each effect dispatches, so a token retired while the call
/// waited, or during a sequence delay, fences that effect.
#[derive(Clone)]
pub struct Fence {
    slot: TokenSlot,
    token: Arc<str>,
}

impl Fence {
    pub fn new(slot: TokenSlot, token: Arc<str>) -> Self {
        Self { slot, token }
    }

    /// Whether the token this fence was admitted under is still in force.
    pub fn holds(&self) -> bool {
        self.slot.is_current(&self.token)
    }
}

impl std::fmt::Debug for Fence {
    // The token is omitted so that it never reaches a log line.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Fence").finish_non_exhaustive()
    }
}

impl PartialEq for Fence {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.token, &other.token)
            && Arc::ptr_eq(&self.slot.current, &other.slot.current)
    }
}

impl Eq for Fence {}

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
        #[serde(default)]
        lease: Option<LeaseClaim>,
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
    RunWakeBatch {
        requests: Vec<WireWake>,
    },
    ObserveResolved {
        terminal_id: Uuid,
        action_key: String,
    },
    ObserveOperatorInput {
        terminal_id: Uuid,
    },
    ClearOnExit {
        terminal_id: Uuid,
    },
}

#[derive(Deserialize)]
struct WireWake {
    result_id: String,
    terminal_id: Uuid,
    clear_action_key: String,
    wake_action_key: String,
    operations: Vec<WireBatchOp>,
}

#[derive(Deserialize)]
struct WireBatchOp {
    kind: Kind,
    payload: String,
    delay_ms: u64,
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
    Wake(Vec<WakeRequest>),
}

/// What an acquired hold is bound to: the lease claim it named (if any), the
/// registry's control state when the hold was taken, the backend token it was
/// admitted under and the row's generation.
#[derive(Clone)]
struct HeldAction {
    terminal_id: Uuid,
    lease: Option<LeaseClaim>,
    control: (Option<Uuid>, u64),
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

    pub async fn handle(&self, token: &Arc<str>, call: Value) -> Value {
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
                    fence: Some(self.fence(token)),
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
                    fence: Some(self.fence(token)),
                };
                self.enqueue(terminal_id, Work::Sequence(request), bytes)
                    .await
            }
            Call::AcquireAction { terminal_id, lease } => {
                self.acquire(terminal_id, lease, token).await
            }
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
            Call::RunWakeBatch { requests } => self.wake_batch(requests, token).await,
            Call::ObserveResolved {
                terminal_id,
                action_key,
            } => settled(
                self.shared
                    .coordinator
                    .observe_resolved(terminal_id, &action_key)
                    .await,
            ),
            Call::ObserveOperatorInput { terminal_id } => settled(
                self.shared
                    .coordinator
                    .observe_operator_input(terminal_id)
                    .await,
            ),
            Call::ClearOnExit { terminal_id } => {
                settled(self.shared.coordinator.clear_on_exit(terminal_id).await)
            }
        }
    }

    /// The fence for a call admitted under `token`: its effects dispatch only
    /// while that token is still the one in force.
    fn fence(&self, token: &Arc<str>) -> Fence {
        Fence::new(self.token.clone(), Arc::clone(token))
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
            || request
                .lease
                .as_ref()
                .is_some_and(|lease| Some(lease) != held.lease.as_ref())
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
        // Every held operation re-reads the control state the hold bound, for
        // any origin, so control moving under a hold refuses its writes.
        if self.shared.coordinator.control(terminal).await != held.control {
            return encode_refusal(&Refusal::StaleLease);
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

    async fn acquire(&self, terminal: Uuid, lease: Option<LeaseClaim>, token: &Arc<str>) -> Value {
        if !self.token_is(token) {
            return refused(Stage::None, "retired_token", "the write token was retired");
        }
        let coordinator = &self.shared.coordinator;
        // Read before the lease is revalidated and before any wait, so the hold
        // never binds a control state newer than the lease it was checked under.
        let control = coordinator.control(terminal).await;
        if let Some(claim) = lease
            && let Err(error) = coordinator
                .revalidate(terminal, Origin::Operator, Some(claim))
                .await
        {
            return encode_refusal(&error);
        }
        let row = match coordinator.require(terminal).await {
            Ok(row) => row,
            Err(error) => return encode_refusal(&error),
        };
        // Unheld work in flight or queued makes acquire wait for it to drain. An
        // active hold, or an uncertain effect that only proof can settle, refuses at once.
        let deadline = Instant::now() + QUEUE_MAX_AGE;
        let hold = loop {
            let taken = {
                let mut gates = lock(&self.shared.gates);
                let gate = gates.entry(terminal).or_default();
                gate.acquire(Instant::now())
                    .map_err(|error| (error, !gate.is_held() && gate.uncertainty().is_none()))
            };
            match taken {
                Ok(hold) => break hold,
                Err((HoldError::Busy, true)) if Instant::now() < deadline => {
                    tokio::time::sleep(ADMIT_POLL).await;
                    pump(&self.shared, terminal);
                }
                Err((error, _)) => return hold_refusal(error),
            }
        };
        let held = HeldAction {
            terminal_id: terminal,
            lease,
            control,
            token: Arc::clone(token),
            attempt_generation: row.attempt_generation,
            host_epoch: row.host_epoch,
            hold,
        };
        let id = Uuid::new_v4();
        lock(&self.shared.holds).insert(id, held);
        json!({"status": "held", "hold": {"id": id}})
    }

    async fn wake_batch(&self, requests: Vec<WireWake>, token: &Arc<str>) -> Value {
        let Some(lane) = requests.first().map(|request| request.terminal_id) else {
            return json!({"results": []});
        };
        let bytes: usize = requests
            .iter()
            .flat_map(|request| &request.operations)
            .map(|operation| operation.payload.len())
            .sum();
        let fence = self.fence(token);
        let requests = requests
            .into_iter()
            .map(|request| WakeRequest {
                result_id: request.result_id,
                terminal_id: request.terminal_id,
                clear_action_key: request.clear_action_key,
                wake_action_key: request.wake_action_key,
                operations: request
                    .operations
                    .into_iter()
                    .map(|op| BatchOp {
                        kind: op.kind,
                        payload: op.payload,
                        delay_ms: op.delay_ms,
                    })
                    .collect(),
                fence: Some(fence.clone()),
            })
            .collect();
        self.enqueue(lane, Work::Wake(requests), bytes).await
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
        let mut admitted = Admitted {
            shared: Arc::clone(&shared),
            terminal,
            effect: Some(effect),
        };
        // A caller that gave up before admission never dispatches its effect.
        let value = if reply.is_closed() {
            None
        } else {
            Some(match work {
                Work::Write(request) => encode_write(shared.coordinator.write(request).await),
                Work::Sequence(request) => {
                    encode_sequence(shared.coordinator.run_sequence(request).await)
                }
                Work::Wake(requests) => run_wake(&shared, terminal, requests).await,
            })
        };
        let mut gates = lock(&shared.gates);
        if let Some(gate) = gates.get_mut(&terminal) {
            let effect = admitted
                .effect
                .take()
                .expect("the admitted effect settles once");
            let settled = match value {
                None => gate.finish(effect, Instant::now()),
                Some(value) => {
                    if reply.send(value).is_ok() {
                        gate.finish(effect, Instant::now())
                    } else {
                        gate.uncertain(effect, UncertainCause::Disconnected)
                    }
                }
            };
            debug_assert!(settled.is_ok(), "the admitted effect is in flight");
        }
    });
}

/// Settles an admitted effect when its work ends, even by panic. An effect that
/// ends without a verdict is uncertain; the route's active count drops either way.
struct Admitted {
    shared: Arc<Shared>,
    terminal: Uuid,
    effect: Option<EffectId>,
}

impl Drop for Admitted {
    fn drop(&mut self) {
        if let Some(effect) = self.effect.take() {
            let mut gates = lock(&self.shared.gates);
            if let Some(gate) = gates.get_mut(&self.terminal) {
                let _ = gate.uncertain(effect, UncertainCause::Panicked);
            }
        }
        pump(&self.shared, self.terminal);
        self.shared.active.fetch_sub(1, Ordering::SeqCst);
    }
}

/// Runs one native wake batch under its lane's admitted effect. Every other
/// target is reserved without waiting, so a hold or unheld write there cannot
/// interleave with the batch; a busy target refuses the whole batch.
async fn run_wake(shared: &Arc<Shared>, lane: Uuid, requests: Vec<WakeRequest>) -> Value {
    let mut targets: Vec<Uuid> = requests
        .iter()
        .map(|request| request.terminal_id)
        .filter(|id| *id != lane)
        .collect();
    targets.sort();
    targets.dedup();
    let mut reserved = Vec::new();
    for id in targets {
        let taken = lock(&shared.gates)
            .entry(id)
            .or_default()
            .acquire(Instant::now());
        match taken {
            Ok(hold) => reserved.push((id, hold)),
            Err(error) => {
                release_reserved(shared, reserved);
                return hold_refusal(error);
            }
        }
    }
    let result = shared.coordinator.run_native_wake_batch(requests).await;
    release_reserved(shared, reserved);
    encode_wake(result)
}

fn release_reserved(shared: &Arc<Shared>, reserved: Vec<(Uuid, HoldToken)>) {
    for (id, hold) in reserved {
        let released = lock(&shared.gates)
            .get_mut(&id)
            .map(|gate| gate.release(hold, Instant::now()));
        debug_assert!(
            matches!(released, Some(Ok(()))),
            "a reservation taken in this batch is still held"
        );
        pump(shared, id);
    }
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

fn encode_wake(result: Result<Vec<WakeResult>, Refusal>) -> Value {
    match result {
        Ok(results) => json!({
            "results": results.into_iter().map(encode_wake_result).collect::<Vec<_>>(),
        }),
        Err(error) => encode_refusal(&error),
    }
}

fn encode_wake_result(result: WakeResult) -> Value {
    let step = match result.step {
        WakeStep::Preflight => "preflight",
        WakeStep::Prelude => "prelude",
        WakeStep::Enter => "enter",
    };
    json!({
        "result_id": result.result_id,
        "step": step,
        "outcome": encode_write(result.outcome.map_err(Refusal::Write)),
    })
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
        Refusal::RetiredToken => refused(Stage::None, "retired_token", error.to_string()),
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
