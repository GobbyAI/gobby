//! Reply validation for the Rust worker client (R56): the initialize result, the
//! handshake result, the error object, the byte limits and the frozen corpus
//! vectors run through the reply path. A child of `tests`, so it shares its
//! helpers and the corpus reader.

use serde_json::{Value, json};

use super::{
    CREDENTIAL, ScriptedChannel, case_response, installed_identity, refusal, response, session_at,
    unknown_outcome,
};
use crate::lifecycle::worker_handshake::{
    ErrorBody, HandshakeField, HandshakeResult, MAX_DOMAIN_OP_REQUEST_BYTES,
    MAX_WORKER_REQUEST_BYTES, PhaseReply, ProtocolViolation, UnknownOutcome, WorkerCallError,
    WorkerSession, perform_handshake, perform_initialize, perform_invoke,
};

/// One reply envelope for request generation 7, with `payload` under `key`.
fn envelope(operation_id: &str, key: &str, payload: Value) -> Vec<u8> {
    json!({
        "protocol_version": 1,
        "generation": 7,
        "operation_id": operation_id,
        (key): payload,
    })
    .to_string()
    .into_bytes()
}

fn error_reply(operation_id: &str, code: &str, message: &str) -> Vec<u8> {
    envelope(
        operation_id,
        "error",
        json!({ "code": code, "message": message }),
    )
}

fn phase_result(
    phase: &str,
    status: &str,
    next_phase: Value,
    message: Value,
    error_type: Value,
) -> Value {
    json!({
        "phase": phase,
        "status": status,
        "next_phase": next_phase,
        "message": message,
        "error_type": error_type,
    })
}

/// A valid `ready` result for the first phase.
fn ready_first() -> Value {
    phase_result(
        "storage_and_config",
        "ready",
        json!("content_refusal"),
        Value::Null,
        Value::Null,
    )
}

fn without(mut value: Value, key: &str) -> Value {
    value
        .as_object_mut()
        .expect("the result is an object")
        .remove(key);
    value
}

fn with(mut value: Value, key: &str, field: Value) -> Value {
    value[key] = field;
    value
}

fn session_at_seq(generation: u64, seq: u64) -> WorkerSession {
    let mut session = session_at(generation);
    session.next_seq = seq;
    session
}

/// Runs one initialize for `phase` on a fresh session, answering `result` to its first operation.
async fn answer_initialize(phase: &str, result: Value) -> Result<PhaseReply, WorkerCallError> {
    let mut channel = ScriptedChannel::replying([Ok(envelope("7:1", "result", result))]);
    perform_initialize(&mut channel, &mut session_at(7), phase).await
}

/// The unknown outcome of an initialize whose reply for `phase` fails validation.
async fn initialize_unknown(phase: &str, result: Value) -> UnknownOutcome {
    let err = answer_initialize(phase, result)
        .await
        .expect_err("the reply fails validation");
    unknown_outcome(err)
}

/// The corpus handshake reply with `mutate` applied to its result.
fn handshake_reply_with(mutate: impl FnOnce(&mut Value)) -> Vec<u8> {
    let mut reply: Value =
        serde_json::from_slice(&case_response("handshake accepted")).expect("corpus reply parses");
    mutate(&mut reply["result"]);
    reply.to_string().into_bytes()
}

async fn handshake_answer(reply: Vec<u8>) -> Result<HandshakeResult, WorkerCallError> {
    let mut channel = ScriptedChannel::replying([Ok(reply)]);
    perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL).await
}

async fn handshake_answer_error(reply: Vec<u8>) -> WorkerCallError {
    handshake_answer(reply)
        .await
        .expect_err("the reply is an error")
}

async fn invoke_answer(reply: Vec<u8>) -> Result<Value, WorkerCallError> {
    let mut channel = ScriptedChannel::replying([Ok(reply)]);
    perform_invoke(&mut channel, &mut session_at(7), "anything", json!({})).await
}

// The initialize result has exactly five keys, and each one is required.

#[tokio::test]
async fn initialize_result_missing_phase_is_unknown() {
    assert_eq!(
        initialize_unknown("storage_and_config", without(ready_first(), "phase"))
            .await
            .command,
        "initialize"
    );
}

#[tokio::test]
async fn initialize_result_missing_status_is_unknown() {
    assert_eq!(
        initialize_unknown("storage_and_config", without(ready_first(), "status"))
            .await
            .command,
        "initialize"
    );
}

#[tokio::test]
async fn initialize_result_missing_next_phase_is_unknown() {
    assert_eq!(
        initialize_unknown("storage_and_config", without(ready_first(), "next_phase"))
            .await
            .command,
        "initialize"
    );
}

#[tokio::test]
async fn initialize_result_missing_message_is_unknown() {
    assert_eq!(
        initialize_unknown("storage_and_config", without(ready_first(), "message"))
            .await
            .command,
        "initialize"
    );
}

#[tokio::test]
async fn initialize_result_missing_error_type_is_unknown() {
    assert_eq!(
        initialize_unknown("storage_and_config", without(ready_first(), "error_type"))
            .await
            .command,
        "initialize"
    );
}

#[tokio::test]
async fn initialize_result_extra_key_is_unknown() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "extra", json!(true)),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn initialize_phase_mismatch_is_unknown() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "phase", json!("content_refusal")),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn initialize_unknown_status_is_unknown() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "status", json!("bogus")),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

// Per-status table: ready rows.

#[tokio::test]
async fn ready_row_next_phase_must_be_the_successor() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "next_phase", json!("startup_content")),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn ready_row_next_phase_must_not_be_null_before_the_last_phase() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "next_phase", Value::Null),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn ready_row_last_phase_must_have_null_next_phase() {
    let last = phase_result(
        "runtime_services",
        "ready",
        json!("content_refusal"),
        Value::Null,
        Value::Null,
    );
    assert_eq!(
        initialize_unknown("runtime_services", last).await.command,
        "initialize"
    );
}

#[tokio::test]
async fn ready_row_message_must_be_null() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "message", json!("unexpected")),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn ready_row_error_type_must_be_null() {
    let outcome = initialize_unknown(
        "storage_and_config",
        with(ready_first(), "error_type", json!("RuntimeError")),
    )
    .await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn ready_for_an_unknown_phase_is_unknown() {
    let result = phase_result("bogus", "ready", Value::Null, Value::Null, Value::Null);
    assert_eq!(
        initialize_unknown("bogus", result).await.command,
        "initialize"
    );
}

// Per-status table: refused rows.

#[tokio::test]
async fn refused_row_next_phase_must_be_null() {
    let refused = phase_result(
        "storage_and_config",
        "refused",
        json!("content_refusal"),
        json!("refused"),
        Value::Null,
    );
    let outcome = initialize_unknown("storage_and_config", refused).await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn refused_row_error_type_must_be_null() {
    let refused = phase_result(
        "storage_and_config",
        "refused",
        Value::Null,
        json!("refused"),
        json!("RuntimeError"),
    );
    let outcome = initialize_unknown("storage_and_config", refused).await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn refused_row_message_must_be_a_string() {
    let refused = phase_result(
        "storage_and_config",
        "refused",
        Value::Null,
        Value::Null,
        Value::Null,
    );
    let outcome = initialize_unknown("storage_and_config", refused).await;
    assert_eq!(outcome.command, "initialize");
}

// Per-status table: failed rows.

#[tokio::test]
async fn failed_row_next_phase_must_be_null() {
    let failed = phase_result(
        "startup_content",
        "failed",
        json!("config_runtime"),
        json!("boom"),
        json!("RuntimeError"),
    );
    let outcome = initialize_unknown("startup_content", failed).await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn failed_row_message_must_be_a_string() {
    let failed = phase_result(
        "startup_content",
        "failed",
        Value::Null,
        Value::Null,
        json!("RuntimeError"),
    );
    let outcome = initialize_unknown("startup_content", failed).await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn failed_row_error_type_must_be_a_string() {
    let failed = phase_result(
        "startup_content",
        "failed",
        Value::Null,
        json!("boom"),
        Value::Null,
    );
    let outcome = initialize_unknown("startup_content", failed).await;
    assert_eq!(outcome.command, "initialize");
}

#[tokio::test]
async fn failed_row_error_type_must_not_be_empty() {
    let failed = phase_result(
        "startup_content",
        "failed",
        Value::Null,
        json!("boom"),
        json!(""),
    );
    let outcome = initialize_unknown("startup_content", failed).await;
    assert_eq!(outcome.command, "initialize");
}

/// Messages are capped at 2,048 characters, counted as characters and not bytes.
#[tokio::test]
async fn phase_message_limit_counts_characters() {
    let at_limit = phase_result(
        "content_refusal",
        "refused",
        Value::Null,
        json!("é".repeat(2048)),
        Value::Null,
    );
    let reply = answer_initialize("content_refusal", at_limit)
        .await
        .expect("2,048 characters are within the limit");
    assert_eq!(reply.message.map(|text| text.chars().count()), Some(2048));

    let over_limit = phase_result(
        "content_refusal",
        "refused",
        Value::Null,
        json!("é".repeat(2049)),
        Value::Null,
    );
    assert_eq!(
        initialize_unknown("content_refusal", over_limit)
            .await
            .command,
        "initialize"
    );
}

// Error objects.

#[tokio::test]
async fn error_code_outside_the_contract_is_unknown_for_initialize() {
    let mut channel = ScriptedChannel::replying([Ok(error_reply("7:1", "bogus", "x"))]);
    let err = perform_initialize(&mut channel, &mut session_at(7), "storage_and_config")
        .await
        .expect_err("the code is outside the contract");
    assert_eq!(unknown_outcome(err).command, "initialize");
}

#[tokio::test]
async fn error_code_outside_the_contract_is_protocol_violation_for_handshake() {
    let mut channel = ScriptedChannel::replying([Ok(error_reply("7:1", "bogus", "x"))]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("the code is outside the contract");
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );
}

#[tokio::test]
async fn error_message_must_be_the_fixed_text_for_its_code() {
    let reply = error_reply("7:1", "unauthorized", "nope");
    let err = handshake_answer_error(reply).await;
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );
}

#[tokio::test]
async fn op_failed_message_over_the_limit_is_unknown_for_invoke() {
    let reply = error_reply("7:1", "op_failed", &"x".repeat(2049));
    let err = invoke_answer(reply)
        .await
        .expect_err("the message is over 2,048 characters");
    assert_eq!(unknown_outcome(err).command, "invoke");
}

#[tokio::test]
async fn op_failed_message_at_the_limit_is_accepted() {
    let reply = error_reply("7:1", "op_failed", &"x".repeat(2048));
    match invoke_answer(reply)
        .await
        .expect_err("op_failed is an error")
    {
        WorkerCallError::OpFailed(body) => {
            assert_eq!(body.message.chars().count(), 2048);
        }
        other => panic!("expected OpFailed, got {other:?}"),
    }
}

#[tokio::test]
async fn invoke_op_failed_is_its_own_variant() {
    let reply = error_reply("7:1", "op_failed", "boom");
    match invoke_answer(reply)
        .await
        .expect_err("op_failed is an error")
    {
        WorkerCallError::OpFailed(body) => assert_eq!(
            body,
            ErrorBody {
                code: "op_failed".to_owned(),
                message: "boom".to_owned(),
            }
        ),
        other => panic!("expected OpFailed, got {other:?}"),
    }
}

#[tokio::test]
async fn invoke_not_ready_stays_refused() {
    let reply = error_reply("7:1", "not_ready", "worker is not ready");
    let err = invoke_answer(reply)
        .await
        .expect_err("not ready is an error");
    assert_eq!(refusal(err).code, "not_ready");
}

#[tokio::test]
async fn handshake_op_failed_stays_refused() {
    let reply = error_reply("7:1", "op_failed", "schema identity unavailable");
    let err = handshake_answer_error(reply).await;
    let body = refusal(err);
    assert_eq!(body.code, "op_failed");
    assert_eq!(body.message, "schema identity unavailable");
}

// Error codes per command. The contract's guard table gives each command its own set of
// codes. A code outside a command's set is a violation: Protocol for a handshake, and an
// unknown outcome for initialize and invoke.

/// Answers one error reply to a handshake.
async fn handshake_error(code: &str, message: &str) -> WorkerCallError {
    handshake_answer_error(error_reply("7:1", code, message)).await
}

/// Answers one error reply to an initialize for the first phase.
async fn initialize_error(code: &str, message: &str) -> WorkerCallError {
    let mut channel = ScriptedChannel::replying([Ok(error_reply("7:1", code, message))]);
    perform_initialize(&mut channel, &mut session_at(7), "storage_and_config")
        .await
        .expect_err("an error reply is an error")
}

/// Answers one error reply to an invoke.
async fn invoke_error(code: &str, message: &str) -> WorkerCallError {
    invoke_answer(error_reply("7:1", code, message))
        .await
        .expect_err("an error reply is an error")
}

#[tokio::test]
async fn handshake_accepts_its_own_error_codes() {
    let cases = [
        ("invalid_request", "invalid request"),
        ("protocol_mismatch", "protocol version not accepted"),
        ("unauthorized", "credential refused"),
        (
            "stale_generation",
            "generation does not match the bound generation",
        ),
        ("op_failed", "schema identity unavailable"),
    ];
    for (code, message) in cases {
        let body = refusal(handshake_error(code, message).await);
        assert_eq!(body.code, code);
        assert_eq!(body.message, message);
    }
}

#[tokio::test]
async fn handshake_error_code_outside_its_set_is_protocol_violation() {
    let cases = [
        ("not_handshaken", "no handshake for this worker"),
        ("unknown_command", "unknown command"),
        ("phase_out_of_order", "phase is not the next phase"),
        ("worker_unusable", "worker is unusable"),
        ("not_ready", "worker is not ready"),
    ];
    for (code, message) in cases {
        let err = handshake_error(code, message).await;
        assert!(
            matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
            "{code}: {err:?}"
        );
    }
}

#[tokio::test]
async fn initialize_accepts_its_own_error_codes() {
    let cases = [
        ("invalid_request", "invalid request"),
        ("protocol_mismatch", "protocol version not accepted"),
        ("not_handshaken", "no handshake for this worker"),
        (
            "stale_generation",
            "generation does not match the bound generation",
        ),
        ("worker_unusable", "worker is unusable"),
        ("phase_out_of_order", "phase is not the next phase"),
    ];
    for (code, message) in cases {
        let body = refusal(initialize_error(code, message).await);
        assert_eq!(body.code, code);
        assert_eq!(body.message, message);
    }
}

#[tokio::test]
async fn initialize_error_code_outside_its_set_is_unknown() {
    let cases = [
        ("unauthorized", "credential refused"),
        ("unknown_command", "unknown command"),
        ("not_ready", "worker is not ready"),
        ("op_failed", "boom"),
    ];
    for (code, message) in cases {
        let outcome = unknown_outcome(initialize_error(code, message).await);
        assert_eq!(outcome.command, "initialize", "{code}");
        assert_eq!(outcome.operation_id, "7:1", "{code}");
    }
}

/// An `op_failed` reply to initialize is the unknown outcome, never a refusal (R66).
#[tokio::test]
async fn initialize_op_failed_is_unknown() {
    let outcome = unknown_outcome(initialize_error("op_failed", "boom").await);
    assert_eq!(
        outcome,
        UnknownOutcome {
            command: "initialize",
            generation: 7,
            operation_id: "7:1".to_owned(),
        }
    );
}

#[tokio::test]
async fn invoke_accepts_its_own_error_codes() {
    let cases = [
        ("invalid_request", "invalid request"),
        ("protocol_mismatch", "protocol version not accepted"),
        ("not_handshaken", "no handshake for this worker"),
        (
            "stale_generation",
            "generation does not match the bound generation",
        ),
        ("worker_unusable", "worker is unusable"),
        ("not_ready", "worker is not ready"),
        ("unknown_command", "unknown command"),
    ];
    for (code, message) in cases {
        let body = refusal(invoke_error(code, message).await);
        assert_eq!(body.code, code);
        assert_eq!(body.message, message);
    }
    match invoke_error("op_failed", "boom").await {
        WorkerCallError::OpFailed(body) => assert_eq!(body.code, "op_failed"),
        other => panic!("expected OpFailed, got {other:?}"),
    }
}

#[tokio::test]
async fn invoke_error_code_outside_its_set_is_unknown() {
    let cases = [
        ("unauthorized", "credential refused"),
        ("phase_out_of_order", "phase is not the next phase"),
    ];
    for (code, message) in cases {
        let outcome = unknown_outcome(invoke_error(code, message).await);
        assert_eq!(outcome.command, "invoke", "{code}");
        assert_eq!(outcome.operation_id, "7:1", "{code}");
    }
}

// Failed phase outcomes the Python side sends. Each is a result with status failed, not an error.

#[tokio::test]
async fn initialize_failed_incomplete_create_is_a_failed_outcome() {
    let failed = phase_result(
        "runtime_services",
        "failed",
        Value::Null,
        json!("create returned before every phase ran"),
        json!("IncompleteCreate"),
    );
    let reply = answer_initialize("runtime_services", failed)
        .await
        .expect("a failed phase is a result");
    assert_eq!(reply.status, "failed");
    assert_eq!(reply.next_phase, None);
    assert_eq!(
        reply.message.as_deref(),
        Some("create returned before every phase ran")
    );
    assert_eq!(reply.error_type.as_deref(), Some("IncompleteCreate"));
}

#[tokio::test]
async fn initialize_failed_phase_order_violation_is_a_failed_outcome() {
    let failed = phase_result(
        "startup_content",
        "failed",
        Value::Null,
        json!("create entered a phase out of order"),
        json!("PhaseOrderViolation"),
    );
    let reply = answer_initialize("startup_content", failed)
        .await
        .expect("a failed phase is a result");
    assert_eq!(reply.status, "failed");
    assert_eq!(reply.next_phase, None);
    assert_eq!(
        reply.message.as_deref(),
        Some("create entered a phase out of order")
    );
    assert_eq!(reply.error_type.as_deref(), Some("PhaseOrderViolation"));
}

// Handshake result checks.

#[tokio::test]
async fn handshake_protocol_version_must_be_one() {
    let reply = handshake_reply_with(|result| result["protocol_version"] = json!(2));
    let err = handshake_answer_error(reply).await;
    assert!(
        matches!(
            err,
            WorkerCallError::Protocol(ProtocolViolation::ResultMismatch(
                HandshakeField::ProtocolVersion
            ))
        ),
        "{err:?}"
    );
}

#[tokio::test]
async fn handshake_result_generation_must_match_the_session() {
    let reply = handshake_reply_with(|result| result["generation"] = json!(8));
    let err = handshake_answer_error(reply).await;
    assert!(
        matches!(
            err,
            WorkerCallError::Protocol(ProtocolViolation::ResultMismatch(
                HandshakeField::Generation
            ))
        ),
        "{err:?}"
    );
}

#[tokio::test]
async fn handshake_commands_must_be_exact_and_ordered() {
    let wrong = [
        json!(["handshake", "invoke"]),
        json!(["invoke", "initialize", "handshake"]),
    ];
    for commands in wrong {
        let reply = handshake_reply_with(|result| result["commands"] = commands.clone());
        let err = handshake_answer_error(reply).await;
        assert!(
            matches!(
                err,
                WorkerCallError::Protocol(ProtocolViolation::ResultMismatch(
                    HandshakeField::Commands
                ))
            ),
            "{commands}: {err:?}"
        );
    }
}

#[tokio::test]
async fn handshake_schema_identity_must_match_installed() {
    let mut identity = installed_identity();
    identity["latest_checksum"] = json!("0".repeat(64));
    let reply = handshake_reply_with(|result| result["schema_identity"] = identity.clone());
    let result = handshake_answer(reply).await;
    assert!(
        matches!(
            result,
            Err(WorkerCallError::Protocol(
                ProtocolViolation::ResultMismatch(HandshakeField::SchemaIdentity)
            ))
        ),
        "{result:?}"
    );
}

#[tokio::test]
async fn handshake_schema_identity_matching_installed_is_accepted() {
    let identity = installed_identity();
    let reply = handshake_reply_with(|result| result["schema_identity"] = identity.clone());
    let result = handshake_answer(reply)
        .await
        .expect("the installed identity is accepted");
    assert_eq!(result.schema_identity, identity);
}

#[tokio::test]
async fn reply_integers_outside_64_bits_are_malformed() {
    let too_big = br#"{"generation":18446744073709551616,"operation_id":"7:1","protocol_version":1,"result":{}}"#;
    let mut channel = ScriptedChannel::replying([Ok(too_big.to_vec())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("a generation beyond 64 bits");
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );

    let negative = br#"{"generation":-1,"operation_id":"7:1","protocol_version":1,"result":{}}"#;
    let mut channel = ScriptedChannel::replying([Ok(negative.to_vec())]);
    let err = perform_handshake(&mut channel, &mut session_at(7), CREDENTIAL)
        .await
        .expect_err("a negative generation");
    assert!(
        matches!(err, WorkerCallError::Protocol(ProtocolViolation::Malformed)),
        "{err:?}"
    );
}

/// A success reply to invoke whose result holds an integer outside `[-2^63, 2^64 - 1]` is a
/// protocol violation with an unknown outcome. The Python side never sends such a
/// result: it answers `op_failed` instead, so a success reply that carries one is malformed.
#[tokio::test]
async fn invoke_result_integer_beyond_64_bits_is_unknown() {
    let too_big: &[u8] = br#"{"protocol_version":1,"generation":7,"operation_id":"7:1","result":{"nested":{"count":18446744073709551616}}}"#;
    let too_small: &[u8] = br#"{"protocol_version":1,"generation":7,"operation_id":"7:1","result":{"nested":{"count":-9223372036854775809}}}"#;
    for reply in [too_big, too_small] {
        let err = invoke_answer(reply.to_vec())
            .await
            .expect_err("an integer outside [-2^63, 2^64 - 1] in the result");
        let outcome = unknown_outcome(err);
        assert_eq!(outcome.command, "invoke");
        assert_eq!(outcome.operation_id, "7:1");
    }
}

/// The two byte limits are pinned to their literal values, so a constant change fails a test.
#[test]
fn byte_limits_are_pinned_to_literal_values() {
    assert_eq!(MAX_WORKER_REQUEST_BYTES, 65_536);
    assert_eq!(MAX_DOMAIN_OP_REQUEST_BYTES, 16_777_216);
}

// Frozen corpus vectors through the reply path. Each vector carries its own
// operation id, so the session's sequence is advanced to match it.

#[tokio::test]
async fn corpus_refused_vector_is_a_refused_phase() {
    let mut channel = ScriptedChannel::replying([Ok(response("refused"))]);
    let reply = perform_initialize(&mut channel, &mut session_at_seq(7, 2), "content_refusal")
        .await
        .expect("a refused phase is a result");
    assert_eq!(reply.status, "refused");
    assert_eq!(reply.next_phase, None);
    assert_eq!(reply.message.as_deref(), Some("bundled content is refused"));
    assert_eq!(reply.error_type, None);
}

#[tokio::test]
async fn corpus_failed_vector_is_a_failed_phase() {
    let mut channel = ScriptedChannel::replying([Ok(response("failed"))]);
    let reply = perform_initialize(&mut channel, &mut session_at_seq(7, 3), "startup_content")
        .await
        .expect("a failed phase is a result");
    assert_eq!(reply.status, "failed");
    assert_eq!(reply.next_phase, None);
    assert_eq!(reply.message.as_deref(), Some("boom"));
    assert_eq!(reply.error_type.as_deref(), Some("RuntimeError"));
}

#[tokio::test]
async fn corpus_last_phase_vector_has_null_next_phase() {
    let mut channel = ScriptedChannel::replying([Ok(response("ready on last phase"))]);
    let reply = perform_initialize(&mut channel, &mut session_at_seq(7, 5), "runtime_services")
        .await
        .expect("a ready last phase is a result");
    assert_eq!(reply.status, "ready");
    assert_eq!(reply.next_phase, None);
    assert_eq!(reply.message, None);
    assert_eq!(reply.error_type, None);
}

#[tokio::test]
async fn corpus_worker_unusable_vector_is_refused() {
    let mut channel = ScriptedChannel::replying([Ok(response("worker_unusable"))]);
    let err = perform_initialize(&mut channel, &mut session_at_seq(7, 4), "config_runtime")
        .await
        .expect_err("worker_unusable is an error");
    let body = refusal(err);
    assert_eq!(body.code, "worker_unusable");
    assert_eq!(body.message, "worker is unusable");
}
