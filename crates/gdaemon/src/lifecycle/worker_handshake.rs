//! Rust side of the private worker protocol (R0 frozen contract, protocol v1).
//!
//! Dormant in R0: `RUNNER_DEFAULT` is Python, so the supervisor performs no
//! exchange, and R1 wires the call site. Each call makes exactly one exchange
//! and never resends. A reply lost on any command is an unknown outcome for the
//! caller to reconcile. Requests are built here, so their bytes equal the
//! corpus bytes (`tests/contracts/worker_protocol.json`).

use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

#[cfg(unix)]
use super::backend::BackendSupervisor;

/// The protocol version this client speaks.
pub const WORKER_PROTOCOL_VERSION: u32 = 1;
/// The protocol versions the worker accepts. Corpus blocks match this set.
pub const ACCEPTED_WORKER_PROTOCOL_VERSIONS: &[u32] = &[1];
/// Bound for handshake and initialize requests.
pub const MAX_WORKER_REQUEST_BYTES: usize = 65_536;
/// Bound for invoke requests (the hook route's bound).
pub const MAX_DOMAIN_OP_REQUEST_BYTES: usize = 16 * 1024 * 1024;

const HANDSHAKE: &str = "handshake";
const INITIALIZE: &str = "initialize";
const INVOKE: &str = "invoke";
const ENVELOPE_KEYS: [&str; 5] = [
    "protocol_version",
    "generation",
    "operation_id",
    "result",
    "error",
];

/// Which runner the worker serves. The compiled default is Python until the cutover commit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunnerMode {
    Python,
    Rust,
}

/// The compiled runner default. Its activation commit sets it to `Rust`.
pub const RUNNER_DEFAULT: RunnerMode = RunnerMode::Python;

/// Assigns worker generations. The first value is 1, so a generation is never 0.
#[derive(Debug)]
pub struct GenerationSource(AtomicU64);

impl Default for GenerationSource {
    fn default() -> Self {
        Self(AtomicU64::new(1))
    }
}

impl GenerationSource {
    /// Returns the current generation and advances the source.
    pub fn next_generation(&self) -> u64 {
        self.0.fetch_add(1, Ordering::Relaxed)
    }
}

/// Why a worker session could not be created.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionError {
    ZeroGeneration,
}

/// One worker generation's client state: the generation and the operation sequence.
#[derive(Debug)]
pub struct WorkerSession {
    generation: u64,
    next_seq: u64,
}

impl WorkerSession {
    /// Starts a session for `generation`, which must be nonzero.
    pub fn new(generation: u64) -> Result<Self, SessionError> {
        if generation == 0 {
            return Err(SessionError::ZeroGeneration);
        }
        Ok(Self {
            generation,
            next_seq: 1,
        })
    }

    /// Starts a session for the generation the source assigns next.
    pub fn spawn(source: &GenerationSource) -> Result<Self, SessionError> {
        Self::new(source.next_generation())
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Returns `<generation>:<seq>` and advances the sequence. The sequence starts at 1.
    fn next_operation_id(&mut self) -> String {
        let generation = self.generation;
        let seq = self.next_seq;
        self.next_seq += 1;
        format!("{generation}:{seq}")
    }
}

/// The reply was lost: the transport returned no bytes for the request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChannelError;

/// One request-reply exchange with the worker. A caller performs one exchange per command.
pub trait WorkerChannel {
    fn exchange(
        &mut self,
        request: &[u8],
    ) -> impl Future<Output = Result<Vec<u8>, ChannelError>> + Send;
}

/// A command whose reply was lost. The effect may have happened, and nothing was resent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnknownOutcome {
    pub command: &'static str,
    pub generation: u64,
    pub operation_id: String,
}

/// The error object of a refused reply.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorBody {
    pub code: String,
    pub message: String,
}

/// A reply that failed envelope or handshake validation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolViolation {
    ProtocolMismatch { got: u64 },
    GenerationMismatch { expected: u64, got: u64 },
    OperationMismatch,
    Malformed,
}

/// Why a worker call did not produce a result.
#[derive(Debug)]
pub enum WorkerCallError {
    /// The handshake credential was empty. Nothing was sent.
    CredentialMissing,
    /// The request exceeded its bound. Nothing was sent.
    RequestTooLarge { bytes: usize, limit: usize },
    /// The reply was lost, or it failed validation after an effectful command.
    Unknown(UnknownOutcome),
    /// A handshake reply failed validation.
    Protocol(ProtocolViolation),
    /// The worker answered with an error.
    Refused(ErrorBody),
}

/// The handshake result. `schema_identity` is returned as JSON and compared by R1.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HandshakeResult {
    pub protocol_version: u32,
    pub generation: u64,
    pub commands: Vec<String>,
    pub schema_identity: Value,
}

/// The initialize result for one phase.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PhaseReply {
    pub phase: String,
    pub status: String,
    pub next_phase: Option<String>,
    pub message: Option<String>,
    pub error_type: Option<String>,
}

/// The outcome of the supervisor's handshake for a runner mode.
#[derive(Debug)]
pub enum WorkerHandshake {
    /// The mode performs no exchange (the Python default).
    Dormant,
    Ready(HandshakeResult),
}

/// A reply whose envelope passed validation, with its one payload.
enum Reply {
    Success(Value),
    Refusal(Value),
}

/// Sends the handshake. The credential must be nonempty.
pub async fn perform_handshake<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    credential: &str,
) -> Result<HandshakeResult, WorkerCallError> {
    if credential.is_empty() {
        return Err(WorkerCallError::CredentialMissing);
    }
    round_trip(
        channel,
        session,
        HANDSHAKE,
        json!({ "credential": credential }),
        MAX_WORKER_REQUEST_BYTES,
    )
    .await
}

/// Sends the construction command for one initialize phase.
pub async fn perform_initialize<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    phase: &str,
) -> Result<PhaseReply, WorkerCallError> {
    round_trip(
        channel,
        session,
        INITIALIZE,
        json!({ "phase": phase }),
        MAX_WORKER_REQUEST_BYTES,
    )
    .await
}

/// Sends a domain op invocation and returns its encoded result.
pub async fn perform_invoke<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    op: &str,
    params: Value,
) -> Result<Value, WorkerCallError> {
    round_trip(
        channel,
        session,
        INVOKE,
        json!({ "op": op, "params": params }),
        MAX_DOMAIN_OP_REQUEST_BYTES,
    )
    .await
}

/// Builds one request, checks its bound, makes one exchange and validates the reply.
async fn round_trip<C, T>(
    channel: &mut C,
    session: &mut WorkerSession,
    command: &'static str,
    body: Value,
    limit: usize,
) -> Result<T, WorkerCallError>
where
    C: WorkerChannel,
    T: DeserializeOwned,
{
    let generation = session.generation;
    let operation_id = session.next_operation_id();
    let request = json!({
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "generation": generation,
        "operation_id": operation_id,
        "command": command,
        "body": body,
    })
    .to_string()
    .into_bytes();
    if request.len() > limit {
        return Err(WorkerCallError::RequestTooLarge {
            bytes: request.len(),
            limit,
        });
    }
    let reply_bytes = match channel.exchange(&request).await {
        Ok(bytes) => bytes,
        Err(ChannelError) => return Err(unknown(command, generation, &operation_id)),
    };
    let reply = match read_reply(&reply_bytes, generation, &operation_id) {
        Ok(reply) => reply,
        Err(violation) => {
            return Err(reply_violation(
                command,
                generation,
                &operation_id,
                violation,
            ));
        }
    };
    match reply {
        Reply::Refusal(error) => match serde_json::from_value::<ErrorBody>(error) {
            Ok(body) => Err(WorkerCallError::Refused(body)),
            Err(_) => Err(reply_violation(
                command,
                generation,
                &operation_id,
                ProtocolViolation::Malformed,
            )),
        },
        Reply::Success(value) => serde_json::from_value::<T>(value).map_err(|_| {
            reply_violation(
                command,
                generation,
                &operation_id,
                ProtocolViolation::Malformed,
            )
        }),
    }
}

/// Validates the reply envelope in the order the contract states.
fn read_reply(
    bytes: &[u8],
    generation: u64,
    operation_id: &str,
) -> Result<Reply, ProtocolViolation> {
    let parsed: Value = serde_json::from_slice(bytes).map_err(|_| ProtocolViolation::Malformed)?;
    let Value::Object(mut fields) = parsed else {
        return Err(ProtocolViolation::Malformed);
    };
    if !fields
        .keys()
        .all(|key| ENVELOPE_KEYS.contains(&key.as_str()))
    {
        return Err(ProtocolViolation::Malformed);
    }
    let reply = match (fields.remove("result"), fields.remove("error")) {
        (Some(value), None) => Reply::Success(value),
        (None, Some(value)) => Reply::Refusal(value),
        _ => return Err(ProtocolViolation::Malformed),
    };
    let version = fields
        .get("protocol_version")
        .and_then(Value::as_u64)
        .ok_or(ProtocolViolation::Malformed)?;
    if version != u64::from(WORKER_PROTOCOL_VERSION) {
        return Err(ProtocolViolation::ProtocolMismatch { got: version });
    }
    let got = fields
        .get("generation")
        .and_then(Value::as_u64)
        .ok_or(ProtocolViolation::Malformed)?;
    if got != generation {
        return Err(ProtocolViolation::GenerationMismatch {
            expected: generation,
            got,
        });
    }
    match fields.get("operation_id").and_then(Value::as_str) {
        Some(id) if id == operation_id => Ok(reply),
        Some(_) => Err(ProtocolViolation::OperationMismatch),
        None => Err(ProtocolViolation::Malformed),
    }
}

/// A failed reply is a protocol violation for the handshake and an unknown outcome otherwise.
fn reply_violation(
    command: &'static str,
    generation: u64,
    operation_id: &str,
    violation: ProtocolViolation,
) -> WorkerCallError {
    if command == HANDSHAKE {
        WorkerCallError::Protocol(violation)
    } else {
        unknown(command, generation, operation_id)
    }
}

fn unknown(command: &'static str, generation: u64, operation_id: &str) -> WorkerCallError {
    WorkerCallError::Unknown(UnknownOutcome {
        command,
        generation,
        operation_id: operation_id.to_owned(),
    })
}

#[cfg(unix)]
impl BackendSupervisor {
    /// Runs the handshake for the compiled runner default.
    pub async fn handshake_worker<C: WorkerChannel>(
        &self,
        channel: &mut C,
        session: &mut WorkerSession,
        credential: &str,
    ) -> Result<WorkerHandshake, WorkerCallError> {
        self.handshake_worker_for(RUNNER_DEFAULT, channel, session, credential)
            .await
    }

    /// Runs the handshake for `mode`. Python performs no exchange.
    pub async fn handshake_worker_for<C: WorkerChannel>(
        &self,
        mode: RunnerMode,
        channel: &mut C,
        session: &mut WorkerSession,
        credential: &str,
    ) -> Result<WorkerHandshake, WorkerCallError> {
        match mode {
            RunnerMode::Python => Ok(WorkerHandshake::Dormant),
            RunnerMode::Rust => perform_handshake(channel, session, credential)
                .await
                .map(WorkerHandshake::Ready),
        }
    }
}

#[cfg(test)]
#[path = "worker_handshake/tests.rs"]
mod tests;
