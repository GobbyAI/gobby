//! Tests for the Rust side of the worker handshake (R0 contract, protocol v1).
//!
//! The corpus at `tests/contracts/worker_protocol.json` is frozen. These tests
//! read its exact bytes and never edit it.

use std::collections::VecDeque;
use std::fmt::Debug;

use serde_json::{Value, json};

use super::{
    ACCEPTED_WORKER_PROTOCOL_VERSIONS, ChannelError, ErrorBody, GenerationSource,
    MAX_DOMAIN_OP_REQUEST_BYTES, MAX_WORKER_REQUEST_BYTES, PhaseReply, ProtocolViolation,
    SessionError, UnknownOutcome, WORKER_PROTOCOL_VERSION, WorkerCallError, WorkerChannel,
    WorkerSession, perform_handshake, perform_initialize, perform_invoke,
};
#[cfg(unix)]
use super::{RUNNER_DEFAULT, RunnerMode, WorkerHandshake};
#[cfg(unix)]
use crate::lifecycle::backend::{BackendConfig, BackendSupervisor};
#[cfg(unix)]
use crate::lifecycle::pid_file::{Role, claim_pid_file};

const CREDENTIAL: &str = "corpus-credential";
const CORPUS_PATH: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../tests/contracts/worker_protocol.json"
);

/// A scripted peer. Each exchange pops the next reply, and every request is recorded.
#[derive(Default)]
struct ScriptedChannel {
    replies: VecDeque<Result<Vec<u8>, ChannelError>>,
    requests: Vec<Vec<u8>>,
}

impl ScriptedChannel {
    fn replying(replies: impl IntoIterator<Item = Result<Vec<u8>, ChannelError>>) -> Self {
        Self {
            replies: replies.into_iter().collect(),
            requests: Vec::new(),
        }
    }
}

impl WorkerChannel for ScriptedChannel {
    async fn exchange(&mut self, request: &[u8]) -> Result<Vec<u8>, ChannelError> {
        self.requests.push(request.to_vec());
        self.replies
            .pop_front()
            .unwrap_or_else(|| panic!("exchange with no scripted reply"))
    }
}

fn corpus() -> Value {
    let text = std::fs::read_to_string(CORPUS_PATH).expect("worker protocol corpus is readable");
    serde_json::from_str(&text).expect("worker protocol corpus is JSON")
}

/// The exact bytes of one corpus entry. `list` is `cases` or `responses`.
fn corpus_text(list: &str, name: &str, field: &str) -> Vec<u8> {
    let corpus = corpus();
    let entries = corpus["versions"][0][list]
        .as_array()
        .expect("corpus list is an array");
    let entry = entries
        .iter()
        .find(|entry| entry["name"] == name)
        .unwrap_or_else(|| panic!("corpus has no entry named {name}"));
    entry[field]
        .as_str()
        .expect("corpus bytes are a string")
        .as_bytes()
        .to_vec()
}

fn case_request(name: &str) -> Vec<u8> {
    corpus_text("cases", name, "request_bytes")
}

fn case_response(name: &str) -> Vec<u8> {
    corpus_text("cases", name, "response_bytes")
}

fn response(name: &str) -> Vec<u8> {
    corpus_text("responses", name, "response_bytes")
}

fn session_at(generation: u64) -> WorkerSession {
    WorkerSession::new(generation).expect("generation is nonzero")
}

fn unknown_outcome(error: WorkerCallError) -> UnknownOutcome {
    match error {
        WorkerCallError::Unknown(outcome) => outcome,
        other => panic!("expected an unknown outcome, got {other:?}"),
    }
}

fn refusal(error: WorkerCallError) -> ErrorBody {
    match error {
        WorkerCallError::Refused(body) => body,
        other => panic!("expected a refusal, got {other:?}"),
    }
}

fn too_large<T: Debug>(result: Result<T, WorkerCallError>) -> (usize, usize) {
    match result {
        Err(WorkerCallError::RequestTooLarge { bytes, limit }) => (bytes, limit),
        other => panic!("expected RequestTooLarge, got {other:?}"),
    }
}

fn blob(len: usize) -> Value {
    json!({ "blob": "x".repeat(len) })
}

#[test]
fn session_rejects_generation_zero() {
    assert!(matches!(
        WorkerSession::new(0),
        Err(SessionError::ZeroGeneration)
    ));
}

#[test]
fn generation_source_is_rust_assigned_and_increasing() {
    let source = GenerationSource::default();
    assert_eq!(source.next_generation(), 1);
    assert_eq!(source.next_generation(), 2);
    let session = WorkerSession::spawn(&source).expect("spawned session");
    assert_eq!(session.generation, 3);
}

#[test]
fn generation_source_never_yields_zero() {
    let source = GenerationSource::default();
    let values: Vec<u64> = (0..1_000).map(|_| source.next_generation()).collect();
    assert!(values.iter().all(|value| *value != 0));
    assert!(values.windows(2).all(|pair| pair[0] < pair[1]));
}

#[test]
fn operation_ids_are_generation_colon_seq() {
    let mut session = session_at(7);
    assert_eq!(session.next_operation_id(), "7:1");
    assert_eq!(session.next_operation_id(), "7:2");
}

#[tokio::test]
async fn handshake_request_bytes_match_corpus() {
    let mut channel = ScriptedChannel::replying([Ok(case_response("handshake accepted"))]);
    perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect("accepted handshake");
    assert_eq!(channel.requests, [case_request("handshake accepted")]);
}

#[tokio::test]
async fn initialize_and_invoke_request_bytes_match_corpus() {
    let mut channel = ScriptedChannel::replying([Ok(case_response("initialize before handshake"))]);
    let refused = perform_initialize(&mut channel, &mut session_at(7), "storage_and_config").await;
    assert_eq!(
        refusal(refused.expect_err("corpus refusal")).code,
        "not_handshaken"
    );
    assert_eq!(
        channel.requests,
        [case_request("initialize before handshake")]
    );

    let mut channel = ScriptedChannel::replying([Ok(case_response("command before handshake"))]);
    let refused = perform_invoke(&mut channel, &mut session_at(7), "anything", json!({})).await;
    assert_eq!(
        refusal(refused.expect_err("corpus refusal")).code,
        "not_handshaken"
    );
    assert_eq!(channel.requests, [case_request("command before handshake")]);
}

#[tokio::test]
async fn handshake_accepted_round_trip() {
    let mut channel = ScriptedChannel::replying([Ok(case_response("handshake accepted"))]);
    let result = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect("accepted handshake");
    assert_eq!(result.protocol_version, 1);
    assert_eq!(result.generation, 7);
    assert_eq!(result.commands, ["handshake", "initialize", "invoke"]);
    assert_eq!(result.schema_identity, json!("$schema_identity"));
}

#[tokio::test]
async fn rejects_protocol_mismatch() {
    let reply = br#"{"protocol_version":2,"generation":7,"operation_id":"7:1","result":{}}"#;
    let mut channel = ScriptedChannel::replying([Ok(reply.to_vec())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("mismatched protocol version");
    assert!(
        matches!(
            err,
            WorkerCallError::Protocol(ProtocolViolation::ProtocolMismatch { got: 2 })
        ),
        "{err:?}"
    );

    // The corpus reply for a refused version is a protocol_mismatch error at version 1.
    let mut channel = ScriptedChannel::replying([Ok(case_response("protocol mismatch"))]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("refused protocol version");
    assert_eq!(refusal(err).code, "protocol_mismatch");
}

#[tokio::test]
async fn rejects_generation_mismatch() {
    let reply = br#"{"protocol_version":1,"generation":8,"operation_id":"7:1","result":{}}"#;
    let mut channel = ScriptedChannel::replying([Ok(reply.to_vec())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("reply for another generation");
    assert!(
        matches!(
            err,
            WorkerCallError::Protocol(ProtocolViolation::GenerationMismatch {
                expected: 7,
                got: 8
            })
        ),
        "{err:?}"
    );
}

#[tokio::test]
async fn empty_credential_refused_before_exchange() {
    let mut channel = ScriptedChannel::default();
    let err = perform_handshake(&mut channel, &mut session_at(7), "")
        .await
        .expect_err("empty credential");
    assert!(matches!(err, WorkerCallError::CredentialMissing), "{err:?}");
    assert!(channel.requests.is_empty());
}

#[tokio::test]
async fn request_over_64k_refused_before_exchange() {
    // A refusal reports the whole request size, which gives the envelope overhead.
    let probe = "x".repeat(MAX_WORKER_REQUEST_BYTES);
    let mut channel = ScriptedChannel::default();
    let (bytes, limit) =
        too_large(perform_handshake(&mut channel, &mut session_at(7), &probe).await);
    assert_eq!(limit, MAX_WORKER_REQUEST_BYTES);
    assert!(channel.requests.is_empty());
    let overhead = bytes - probe.len();

    // Exactly the limit is exchanged; one byte more is refused before any exchange.
    let at_limit = "x".repeat(MAX_WORKER_REQUEST_BYTES - overhead);
    let mut channel =
        ScriptedChannel::replying([Ok(case_response("handshake with no credential configured"))]);
    let refused = perform_handshake(&mut channel, &mut session_at(7), &at_limit).await;
    assert_eq!(
        refusal(refused.expect_err("unauthorized")).code,
        "unauthorized"
    );
    assert_eq!(channel.requests.len(), 1);
    assert_eq!(channel.requests[0].len(), MAX_WORKER_REQUEST_BYTES);

    let over = "x".repeat(MAX_WORKER_REQUEST_BYTES - overhead + 1);
    let mut channel = ScriptedChannel::default();
    let (bytes, limit) =
        too_large(perform_handshake(&mut channel, &mut session_at(7), &over).await);
    assert_eq!(
        (bytes, limit),
        (MAX_WORKER_REQUEST_BYTES + 1, MAX_WORKER_REQUEST_BYTES)
    );
    assert!(channel.requests.is_empty());

    // Initialize shares the 64 KiB bound.
    let probe = "x".repeat(MAX_WORKER_REQUEST_BYTES);
    let mut channel = ScriptedChannel::default();
    let (bytes, _) = too_large(perform_initialize(&mut channel, &mut session_at(7), &probe).await);
    let overhead = bytes - probe.len();
    let at_limit = "x".repeat(MAX_WORKER_REQUEST_BYTES - overhead);
    let mut channel = ScriptedChannel::replying([Ok(case_response("initialize before handshake"))]);
    let refused = perform_initialize(&mut channel, &mut session_at(7), &at_limit).await;
    assert_eq!(
        refusal(refused.expect_err("refused")).code,
        "not_handshaken"
    );
    assert_eq!(channel.requests[0].len(), MAX_WORKER_REQUEST_BYTES);
    let over = "x".repeat(MAX_WORKER_REQUEST_BYTES - overhead + 1);
    let mut channel = ScriptedChannel::default();
    let (bytes, _) = too_large(perform_initialize(&mut channel, &mut session_at(7), &over).await);
    assert_eq!(bytes, MAX_WORKER_REQUEST_BYTES + 1);
    assert!(channel.requests.is_empty());
}

#[tokio::test]
async fn invoke_over_16mib_refused_before_exchange() {
    let mut channel = ScriptedChannel::default();
    let (bytes, limit) = too_large(
        perform_invoke(
            &mut channel,
            &mut session_at(7),
            "anything",
            blob(MAX_DOMAIN_OP_REQUEST_BYTES),
        )
        .await,
    );
    assert_eq!(limit, MAX_DOMAIN_OP_REQUEST_BYTES);
    assert!(channel.requests.is_empty());
    let overhead = bytes - MAX_DOMAIN_OP_REQUEST_BYTES;

    let mut channel = ScriptedChannel::replying([Ok(case_response("invoke before ready"))]);
    let refused = perform_invoke(
        &mut channel,
        &mut session_at(7),
        "anything",
        blob(MAX_DOMAIN_OP_REQUEST_BYTES - overhead),
    )
    .await;
    assert_eq!(refusal(refused.expect_err("not ready")).code, "not_ready");
    assert_eq!(channel.requests[0].len(), MAX_DOMAIN_OP_REQUEST_BYTES);

    let mut channel = ScriptedChannel::default();
    let (bytes, _) = too_large(
        perform_invoke(
            &mut channel,
            &mut session_at(7),
            "anything",
            blob(MAX_DOMAIN_OP_REQUEST_BYTES - overhead + 1),
        )
        .await,
    );
    assert_eq!(bytes, MAX_DOMAIN_OP_REQUEST_BYTES + 1);
    assert!(channel.requests.is_empty());
}

#[tokio::test]
async fn initialize_lost_reply_is_unknown_and_not_resent() {
    let mut channel = ScriptedChannel::replying([Err(ChannelError)]);
    let err = perform_initialize(&mut channel, &mut session_at(7), "storage_and_config")
        .await
        .expect_err("lost reply");
    let outcome = unknown_outcome(err);
    assert_eq!(outcome.command, "initialize");
    assert_eq!(outcome.generation, 7);
    assert_eq!(outcome.operation_id, "7:1");
    assert_eq!(channel.requests.len(), 1, "a lost reply is never resent");
}

#[tokio::test]
async fn invoke_lost_reply_is_unknown_and_not_resent() {
    let mut channel = ScriptedChannel::replying([Err(ChannelError)]);
    let err = perform_invoke(&mut channel, &mut session_at(7), "anything", json!({}))
        .await
        .expect_err("lost reply");
    let outcome = unknown_outcome(err);
    assert_eq!(outcome.command, "invoke");
    assert_eq!(outcome.operation_id, "7:1");
    assert_eq!(channel.requests.len(), 1, "a lost reply is never resent");
}

#[tokio::test]
async fn handshake_lost_reply_is_unknown_and_not_resent() {
    let mut channel = ScriptedChannel::replying([Err(ChannelError)]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("lost reply");
    let outcome = unknown_outcome(err);
    assert_eq!(outcome.command, "handshake");
    assert_eq!(outcome.generation, 7);
    assert_eq!(outcome.operation_id, "7:1");
    assert_eq!(channel.requests.len(), 1, "a lost reply is never resent");
}

#[cfg(unix)]
fn supervisor_in(home: &tempfile::TempDir) -> BackendSupervisor {
    let pid_file = home.path().join("gobby.pid");
    let claim = claim_pid_file(&pid_file, Role::Daemon)
        .expect("pid file is writable")
        .expect("fresh pid file is claimable");
    let config = BackendConfig::from_command_json(home.path(), Some(r#"["python3","-c","pass"]"#))
        .expect("backend config");
    BackendSupervisor::with_lease(None, claim, config)
}

#[cfg(unix)]
#[tokio::test]
async fn backend_handshake_dormant_under_python_default() {
    assert_eq!(RUNNER_DEFAULT, RunnerMode::Python);
    let home = tempfile::TempDir::new().expect("temp home");
    let supervisor = supervisor_in(&home);
    let mut channel = ScriptedChannel::default();
    let handshake = supervisor
        .handshake_worker(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect("dormant handshake");
    assert!(
        matches!(handshake, WorkerHandshake::Dormant),
        "{handshake:?}"
    );
    assert!(
        channel.requests.is_empty(),
        "no exchange under the Python default"
    );
}

#[cfg(unix)]
#[tokio::test]
async fn backend_handshake_rust_mode_exchanges_once() {
    let home = tempfile::TempDir::new().expect("temp home");
    let supervisor = supervisor_in(&home);
    let mut channel = ScriptedChannel::replying([Ok(case_response("handshake accepted"))]);
    let handshake = supervisor
        .handshake_worker_for(
            RunnerMode::Rust,
            &mut channel,
            &mut session_at(7),
            CREDENTIAL,
        )
        .await
        .expect("rust handshake");
    match handshake {
        WorkerHandshake::Ready(result) => assert_eq!(result.generation, 7),
        other => panic!("expected Ready, got {other:?}"),
    }
    assert_eq!(channel.requests.len(), 1);
}

#[test]
fn corpus_versions_match_accepted() {
    let corpus = corpus();
    let versions: Vec<u64> = corpus["versions"]
        .as_array()
        .expect("versions is an array")
        .iter()
        .map(|block| {
            block["protocol_version"]
                .as_u64()
                .expect("protocol_version is an integer")
        })
        .collect();
    let accepted: Vec<u64> = ACCEPTED_WORKER_PROTOCOL_VERSIONS
        .iter()
        .map(|version| u64::from(*version))
        .collect();
    assert_eq!(versions, accepted);
    assert!(ACCEPTED_WORKER_PROTOCOL_VERSIONS.contains(&WORKER_PROTOCOL_VERSION));
}

#[tokio::test]
async fn response_rejects_unknown_fields() {
    let mut reply: Value =
        serde_json::from_slice(&case_response("handshake accepted")).expect("corpus reply parses");
    reply["extra"] = json!(true);
    let mut channel = ScriptedChannel::replying([Ok(reply.to_string().into_bytes())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("unknown envelope field");
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );

    let mut reply: Value =
        serde_json::from_slice(&case_response("handshake accepted")).expect("corpus reply parses");
    reply["result"]["extra"] = json!(true);
    let mut channel = ScriptedChannel::replying([Ok(reply.to_string().into_bytes())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("unknown result field");
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );
}

#[tokio::test]
async fn error_reply_is_refused() {
    let mut channel = ScriptedChannel::replying([Ok(case_response("handshake wrong credential"))]);
    let err = perform_handshake(&mut channel, &mut session_at(7), "wrong")
        .await
        .expect_err("refused credential");
    let body = refusal(err);
    assert_eq!(body.code, "unauthorized");
    assert_eq!(body.message, "credential refused");
}

#[tokio::test]
async fn initialize_reply_parses_phase_reply() {
    let mut channel = ScriptedChannel::replying([Ok(response("ready with next phase"))]);
    let reply: PhaseReply =
        perform_initialize(&mut channel, &mut session_at(7), "storage_and_config")
            .await
            .expect("ready phase");
    assert_eq!(reply.phase, "storage_and_config");
    assert_eq!(reply.status, "ready");
    assert_eq!(reply.next_phase.as_deref(), Some("content_refusal"));
    assert_eq!(reply.message, None);
    assert_eq!(reply.error_type, None);
}

#[tokio::test]
async fn invoke_returns_result_value() {
    let reply =
        br#"{"generation":7,"operation_id":"7:1","protocol_version":1,"result":{"ok":true}}"#;
    let mut channel = ScriptedChannel::replying([Ok(reply.to_vec())]);
    let value = perform_invoke(&mut channel, &mut session_at(7), "anything", json!({}))
        .await
        .expect("invoke result");
    assert_eq!(value, json!({ "ok": true }));
}

#[tokio::test]
async fn invalid_reply_is_unknown_for_initialize_and_invoke() {
    // A reply with neither result nor error fails validation after the exchange.
    let invalid = br#"{"generation":7,"operation_id":"7:1","protocol_version":1}"#;
    let mut channel = ScriptedChannel::replying([Ok(invalid.to_vec()), Ok(invalid.to_vec())]);
    let err = perform_initialize(&mut channel, &mut session_at(7), "storage_and_config")
        .await
        .expect_err("invalid reply");
    assert_eq!(unknown_outcome(err).command, "initialize");
    let err = perform_invoke(&mut channel, &mut session_at(7), "anything", json!({}))
        .await
        .expect_err("invalid reply");
    assert_eq!(unknown_outcome(err).command, "invoke");
    assert_eq!(channel.requests.len(), 2);
}

/// Reply-validation tests (R56), in a sibling file so this module stays small.
#[path = "reply_tests.rs"]
mod reply_tests;
