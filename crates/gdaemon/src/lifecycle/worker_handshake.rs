//! Rust side of the private worker protocol (R0 frozen contract, protocol v1).
//!
//! Dormant in R0: `RUNNER_DEFAULT` is Python, so the supervisor performs no
//! exchange, and R1 wires the call site. Each call makes exactly one exchange
//! and never resends. A reply lost on any command is an unknown outcome for the
//! caller to reconcile. Requests are built here, so their bytes equal the
//! corpus bytes (`tests/contracts/worker_protocol.json`). Replies are checked
//! against the contract before they reach the caller: the envelope, the error
//! object, each command's own error codes, the handshake result, and the
//! initialize result's per-status table.

use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Deserialize;
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
const OP_FAILED: &str = "op_failed";
const ENVELOPE_KEYS: [&str; 5] = [
    "protocol_version",
    "generation",
    "operation_id",
    "result",
    "error",
];
/// The handshake result's `commands`, sorted (contract 5.5).
const HANDSHAKE_COMMANDS: [&str; 3] = [HANDSHAKE, INITIALIZE, INVOKE];
/// The initialize phases in declaration order (contract 5.4).
const INIT_PHASES: [&str; 5] = [
    "storage_and_config",
    "content_refusal",
    "startup_content",
    "config_runtime",
    "runtime_services",
];
/// The keys of an initialize result. All five are always present.
const PHASE_REPLY_KEYS: [&str; 5] = ["phase", "status", "next_phase", "message", "error_type"];
/// Messages are capped at this many characters, counted as characters (contract 5.3).
const MAX_MESSAGE_CHARS: usize = 2048;
/// Every error code with its fixed message. `op_failed` carries a dynamic message.
const ERROR_CODES: [(&str, Option<&str>); 10] = [
    ("invalid_request", Some("invalid request")),
    ("protocol_mismatch", Some("protocol version not accepted")),
    ("unauthorized", Some("credential refused")),
    ("not_handshaken", Some("no handshake for this worker")),
    (
        "stale_generation",
        Some("generation does not match the bound generation"),
    ),
    ("unknown_command", Some("unknown command")),
    ("phase_out_of_order", Some("phase is not the next phase")),
    ("worker_unusable", Some("worker is unusable")),
    ("not_ready", Some("worker is not ready")),
    (OP_FAILED, None),
];
/// The error codes the handshake may carry, from the contract's guard table. The envelope
/// steps (`invalid_request`, `protocol_mismatch`) apply to every command.
const HANDSHAKE_ERROR_CODES: [&str; 5] = [
    "invalid_request",
    "protocol_mismatch",
    "unauthorized",
    "stale_generation",
    OP_FAILED,
];
/// The error codes initialize may carry. A failed phase is a result, not an error, so
/// `op_failed` is not in this set.
const INITIALIZE_ERROR_CODES: [&str; 6] = [
    "invalid_request",
    "protocol_mismatch",
    "not_handshaken",
    "stale_generation",
    "worker_unusable",
    "phase_out_of_order",
];
/// The error codes invoke may carry: the guard steps of invoke, and `op_failed` from the
/// error code table.
const INVOKE_ERROR_CODES: [&str; 8] = [
    "invalid_request",
    "protocol_mismatch",
    "not_handshaken",
    "stale_generation",
    "worker_unusable",
    "not_ready",
    "unknown_command",
    OP_FAILED,
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

/// A field of the handshake result that failed its check.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandshakeField {
    ProtocolVersion,
    Generation,
    Commands,
    SchemaIdentity,
}

/// A reply that failed envelope or handshake validation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolViolation {
    ProtocolMismatch { got: u64 },
    GenerationMismatch { expected: u64, got: u64 },
    OperationMismatch,
    ResultMismatch(HandshakeField),
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
    /// The worker answered with an error from the command's own set of codes. A code outside
    /// that set is a violation, never a refusal.
    Refused(ErrorBody),
    /// An invoke answered `op_failed`: the op ran and may have had a partial effect.
    OpFailed(ErrorBody),
}

/// What a failed handshake does to the supervisor: refuse it for good, or end the child so the
/// next spawn starts a new generation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HandshakeOutcome {
    Sticky,
    Crash,
}

/// Classifies a failed handshake. Until the classification lands, every failure is a crash.
pub fn handshake_outcome(_error: &WorkerCallError) -> HandshakeOutcome {
    HandshakeOutcome::Crash
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

/// A reply whose envelope passed validation: its result, or its error object.
enum Reply {
    Success(Value),
    Refusal(ErrorBody),
}

/// One exchange's identity, kept so a failed validation can name its command.
struct Call {
    command: &'static str,
    generation: u64,
    operation_id: String,
}

impl Call {
    fn unknown(&self) -> WorkerCallError {
        WorkerCallError::Unknown(UnknownOutcome {
            command: self.command,
            generation: self.generation,
            operation_id: self.operation_id.clone(),
        })
    }

    /// A handshake reply that fails validation is a protocol violation. For the other
    /// commands the effect may already have happened, so the outcome is unknown.
    fn violation(&self, violation: ProtocolViolation) -> WorkerCallError {
        if self.command == HANDSHAKE {
            WorkerCallError::Protocol(violation)
        } else {
            self.unknown()
        }
    }
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
    let (call, reply) = exchange(
        channel,
        session,
        HANDSHAKE,
        json!({ "credential": credential }),
        MAX_WORKER_REQUEST_BYTES,
    )
    .await?;
    let value = into_result(&call, reply)?;
    let result: HandshakeResult =
        serde_json::from_value(value).map_err(|_| call.violation(ProtocolViolation::Malformed))?;
    check_handshake(&call, &result).map_err(|violation| call.violation(violation))?;
    Ok(result)
}

/// Sends the construction command for one initialize phase.
pub async fn perform_initialize<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    phase: &str,
) -> Result<PhaseReply, WorkerCallError> {
    let (call, reply) = exchange(
        channel,
        session,
        INITIALIZE,
        json!({ "phase": phase }),
        MAX_WORKER_REQUEST_BYTES,
    )
    .await?;
    let value = into_result(&call, reply)?;
    let reply = decode_phase_reply(value).map_err(|violation| call.violation(violation))?;
    check_phase_reply(&reply, phase).map_err(|violation| call.violation(violation))?;
    Ok(reply)
}

/// Sends a domain op invocation and returns its encoded result.
pub async fn perform_invoke<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    op: &str,
    params: Value,
) -> Result<Value, WorkerCallError> {
    let (call, reply) = exchange(
        channel,
        session,
        INVOKE,
        json!({ "op": op, "params": params }),
        MAX_DOMAIN_OP_REQUEST_BYTES,
    )
    .await?;
    into_result(&call, reply)
}

/// Builds one request, checks its bound, makes one exchange and validates the reply.
async fn exchange<C: WorkerChannel>(
    channel: &mut C,
    session: &mut WorkerSession,
    command: &'static str,
    body: Value,
    limit: usize,
) -> Result<(Call, Reply), WorkerCallError> {
    let call = Call {
        command,
        generation: session.generation,
        operation_id: session.next_operation_id(),
    };
    let request = json!({
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "generation": call.generation,
        "operation_id": call.operation_id,
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
    let reply_bytes = channel
        .exchange(&request)
        .await
        .map_err(|ChannelError| call.unknown())?;
    let reply = read_reply(&reply_bytes, &call).map_err(|violation| call.violation(violation))?;
    Ok((call, reply))
}

/// Rejects an integer literal outside `[-2^63, 2^64 - 1]` anywhere in a reply. It runs on the
/// raw bytes before the reply is parsed, because `serde_json::Value` would turn such a literal
/// into a float. String literals are skipped, and a number with a fraction or an exponent is
/// not an integer. A number that does not fit an `i128` is outside the range too.
fn scan_integer_width(bytes: &[u8]) -> Result<(), ProtocolViolation> {
    const LOWEST: i128 = -(1_i128 << 63);
    const HIGHEST: i128 = (1_i128 << 64) - 1;
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'"' => {
                index += 1;
                while index < bytes.len() && bytes[index] != b'"' {
                    index += if bytes[index] == b'\\' { 2 } else { 1 };
                }
                index += 1;
            }
            b'-' | b'0'..=b'9' => {
                let start = index;
                while index < bytes.len()
                    && matches!(bytes[index], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9')
                {
                    index += 1;
                }
                let token = &bytes[start..index];
                if token.iter().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
                    continue;
                }
                let value: i128 = std::str::from_utf8(token)
                    .ok()
                    .and_then(|text| text.parse().ok())
                    .ok_or(ProtocolViolation::Malformed)?;
                if !(LOWEST..=HIGHEST).contains(&value) {
                    return Err(ProtocolViolation::Malformed);
                }
            }
            _ => index += 1,
        }
    }
    Ok(())
}

/// Validates the envelope in the order the contract states, then the error object.
fn read_reply(bytes: &[u8], call: &Call) -> Result<Reply, ProtocolViolation> {
    scan_integer_width(bytes)?;
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
    let payload = match (fields.remove("result"), fields.remove("error")) {
        (Some(value), None) => Ok(value),
        (None, Some(value)) => Err(value),
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
    if got != call.generation {
        return Err(ProtocolViolation::GenerationMismatch {
            expected: call.generation,
            got,
        });
    }
    match fields.get("operation_id").and_then(Value::as_str) {
        Some(id) if id == call.operation_id => {}
        Some(_) => return Err(ProtocolViolation::OperationMismatch),
        None => return Err(ProtocolViolation::Malformed),
    }
    match payload {
        Ok(value) => Ok(Reply::Success(value)),
        Err(error) => check_error(error).map(Reply::Refusal),
    }
}

/// Checks an error object: exactly `code` and `message`, a code from the contract,
/// the fixed text for every code that has one, and a message within the limit.
fn check_error(error: Value) -> Result<ErrorBody, ProtocolViolation> {
    let body: ErrorBody =
        serde_json::from_value(error).map_err(|_| ProtocolViolation::Malformed)?;
    let fixed = ERROR_CODES
        .iter()
        .find(|(code, _)| *code == body.code.as_str())
        .map(|(_, fixed)| *fixed)
        .ok_or(ProtocolViolation::Malformed)?;
    if fixed.is_some_and(|text| text != body.message)
        || body.message.chars().count() > MAX_MESSAGE_CHARS
    {
        return Err(ProtocolViolation::Malformed);
    }
    Ok(body)
}

/// Whether the contract lets `command` carry `code`. A command with no set carries nothing.
fn command_may_carry(command: &str, code: &str) -> bool {
    let codes: &[&str] = match command {
        HANDSHAKE => &HANDSHAKE_ERROR_CODES,
        INITIALIZE => &INITIALIZE_ERROR_CODES,
        INVOKE => &INVOKE_ERROR_CODES,
        _ => return false,
    };
    codes.contains(&code)
}

/// The result of a validated reply, or the error it carries, as the command reports it. An
/// error code outside the command's set is a violation: Protocol for a handshake, and an
/// unknown outcome for initialize and invoke (the effect may have happened).
fn into_result(call: &Call, reply: Reply) -> Result<Value, WorkerCallError> {
    match reply {
        Reply::Success(value) => Ok(value),
        Reply::Refusal(body) if !command_may_carry(call.command, &body.code) => {
            Err(call.violation(ProtocolViolation::Malformed))
        }
        Reply::Refusal(body) if call.command == INVOKE && body.code == OP_FAILED => {
            Err(WorkerCallError::OpFailed(body))
        }
        Reply::Refusal(body) => Err(WorkerCallError::Refused(body)),
    }
}

/// Checks the handshake result's `protocol_version`, `generation` and `commands`.
/// `schema_identity` is returned as JSON and compared by R1.
fn check_handshake(call: &Call, result: &HandshakeResult) -> Result<(), ProtocolViolation> {
    if result.protocol_version != WORKER_PROTOCOL_VERSION {
        return Err(ProtocolViolation::ResultMismatch(
            HandshakeField::ProtocolVersion,
        ));
    }
    if result.generation != call.generation {
        return Err(ProtocolViolation::ResultMismatch(
            HandshakeField::Generation,
        ));
    }
    if result.commands != HANDSHAKE_COMMANDS {
        return Err(ProtocolViolation::ResultMismatch(HandshakeField::Commands));
    }
    Ok(())
}

/// Decodes an initialize result. It must carry exactly the five keys, the optional ones included.
fn decode_phase_reply(value: Value) -> Result<PhaseReply, ProtocolViolation> {
    let Value::Object(fields) = &value else {
        return Err(ProtocolViolation::Malformed);
    };
    let complete = fields.len() == PHASE_REPLY_KEYS.len()
        && PHASE_REPLY_KEYS.iter().all(|key| fields.contains_key(*key));
    if !complete {
        return Err(ProtocolViolation::Malformed);
    }
    let reply: PhaseReply =
        serde_json::from_value(value).map_err(|_| ProtocolViolation::Malformed)?;
    Ok(reply)
}

/// Checks an initialize result against the per-status table (contract, phases and statuses).
/// `ready` names the successor of the requested phase, or null after the last phase.
fn check_phase_reply(reply: &PhaseReply, requested: &str) -> Result<(), ProtocolViolation> {
    if reply.phase != requested {
        return Err(ProtocolViolation::Malformed);
    }
    let fits = match reply.status.as_str() {
        "ready" => {
            let index = INIT_PHASES
                .iter()
                .position(|phase| *phase == requested)
                .ok_or(ProtocolViolation::Malformed)?;
            let successor = INIT_PHASES.get(index + 1).copied();
            reply.next_phase.as_deref() == successor
                && reply.message.is_none()
                && reply.error_type.is_none()
        }
        "refused" => {
            reply.next_phase.is_none()
                && reply.error_type.is_none()
                && message_within_limit(reply.message.as_deref())
        }
        "failed" => {
            reply.next_phase.is_none()
                && reply
                    .error_type
                    .as_deref()
                    .is_some_and(|name| !name.is_empty())
                && message_within_limit(reply.message.as_deref())
        }
        _ => false,
    };
    if fits {
        Ok(())
    } else {
        Err(ProtocolViolation::Malformed)
    }
}

/// A refused or failed phase carries a message of at most 2,048 characters.
fn message_within_limit(message: Option<&str>) -> bool {
    message.is_some_and(|text| text.chars().count() <= MAX_MESSAGE_CHARS)
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
