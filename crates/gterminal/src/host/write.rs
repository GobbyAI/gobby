//! Bounded PTY write control verbs.

use std::collections::HashSet;
use std::time::Duration;

use base64::Engine;
use serde_json::{json, Map, Value};
use tokio::time::{sleep_until, Instant};

use super::events::InputActivity;
use super::helpers::{err, named_key_bytes, s};
use super::state::{HostState, Inner, TerminalSlot};
use crate::protocol::MAX_WRITE_BYTES;

pub const MAX_WRITE_BATCH_TARGETS: usize = 64;
pub const MAX_WRITE_BATCH_OPERATIONS_PER_TARGET: usize = 128;
pub const MAX_WRITE_BATCH_DELAY_MS: u64 = 1_000;
pub const MAX_WRITE_BATCH_TOTAL_DELAY_MS: u64 = 5_000;

struct BatchOperation {
    delay_ms: u64,
    payload: Vec<u8>,
}

struct BatchTarget {
    recipient_id: String,
    host_terminal_id: String,
    operations: Vec<BatchOperation>,
}

struct ScheduledOperation {
    due_ms: u64,
    target_index: usize,
    operation_index: usize,
    host_terminal_id: String,
    payload: Vec<u8>,
}

impl HostState {
    pub async fn write(&self, extra: &Map<String, Value>) -> Value {
        let host_terminal_id = s(extra, "host_terminal_id");
        let kind = s(extra, "kind");
        let encoding = extra
            .get("encoding")
            .and_then(Value::as_str)
            .unwrap_or("utf8-b64");
        if encoding != "utf8-b64" {
            return err("invalid_encoding");
        }
        let data_b64 = s(extra, "data");
        let raw = match base64::engine::general_purpose::STANDARD.decode(data_b64.as_bytes()) {
            Ok(bytes) => bytes,
            Err(_) => return err("invalid_encoding"),
        };
        let text = String::from_utf8_lossy(&raw).into_owned();
        let input = match kind.as_str() {
            "paste" => NativeInput::Paste(text),
            "key" => NativeInput::Bytes(named_key_bytes(&text)),
            _ => {
                let mut data = text.into_bytes();
                if extra
                    .get("submit")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
                {
                    data.push(b'\n');
                }
                NativeInput::Bytes(data)
            }
        };
        let mut inner = self.inner.lock().await;
        let slot = match native_slot_mut(&mut inner, &host_terminal_id) {
            Ok(slot) => slot,
            Err(code) => return err(code),
        };
        match deliver_native(slot, input) {
            // The control verb keeps its bounded-write contract: a saturated or
            // closed PTY writer drops the bytes and still answers written.
            Ok(()) | Err("pty_busy") | Err("terminal_gone") => {
                json!({"ok": true, "written": true})
            }
            Err(code) => err(code),
        }
    }

    /// Grants one daemon attachment id the right to type on `host_terminal_id`
    /// over the frame stream. Replaces any previous holder; unledgered.
    pub async fn grant_input(&self, extra: &Map<String, Value>) -> Value {
        let attachment_id = s(extra, "attachment_id");
        if attachment_id.is_empty() {
            return err("invalid_request");
        }
        let mut inner = self.inner.lock().await;
        let slot = match native_slot_mut(&mut inner, &s(extra, "host_terminal_id")) {
            Ok(slot) => slot,
            Err(code) => return err(code),
        };
        let previous = slot.input_grant.replace(attachment_id);
        json!({"ok": true, "granted": true, "previous": previous})
    }

    /// Clears the input grant on `host_terminal_id` when `attachment_id`
    /// matches the holder, or unconditionally when it is omitted. Unledgered.
    pub async fn revoke_input(&self, extra: &Map<String, Value>) -> Value {
        let attachment_id = extra.get("attachment_id").and_then(Value::as_str);
        let mut inner = self.inner.lock().await;
        let slot = match native_slot_mut(&mut inner, &s(extra, "host_terminal_id")) {
            Ok(slot) => slot,
            Err(code) => return err(code),
        };
        let revoked = match attachment_id {
            Some(holder) if slot.input_grant.as_deref() != Some(holder) => false,
            _ => slot.input_grant.take().is_some(),
        };
        json!({"ok": true, "revoked": revoked})
    }

    /// Delivers `Input`/`Paste` from a frame stream whose attachment is
    /// `attachment_id`. The error is the `InputRefused` code for the stream.
    pub(crate) async fn frame_input(
        &self,
        attachment_id: Option<u64>,
        input: NativeInput,
    ) -> Result<(), &'static str> {
        let Some(attachment_id) = attachment_id else {
            return Err("attach_required");
        };
        let kind = input.kind();
        let bytes = input.len();
        let interrupt = input.interrupt();
        let activity = {
            let inner = self.inner.lock().await;
            let attachment = inner
                .attachments
                .get(&attachment_id)
                .ok_or("terminal_gone")?;
            let identity = inner
                .by_host_id
                .get(&attachment.host_terminal_id)
                .ok_or("terminal_gone")?;
            let slot = inner.terminals.get(identity).ok_or("terminal_gone")?;
            if slot.locator.is_some() {
                return Err("not_native");
            }
            let holder = attachment
                .client_attachment_id
                .as_deref()
                .ok_or("input_not_granted")?;
            if slot.input_grant.as_deref() != Some(holder) {
                return Err("input_not_granted");
            }
            deliver_native(slot, input)?;
            InputActivity {
                terminal_id: slot.identity.terminal_id.clone(),
                host_terminal_id: slot.host_terminal_id.clone(),
                attachment_id: holder.to_owned(),
                kind,
                bytes,
                interrupt,
            }
        };
        self.events.emit_input_activity(activity).await;
        Ok(())
    }

    pub async fn write_batch(&self, extra: &Map<String, Value>) -> Value {
        let Some(raw_targets) = extra.get("targets").and_then(Value::as_array) else {
            return err("invalid_targets");
        };
        if raw_targets.len() > MAX_WRITE_BATCH_TARGETS {
            return err("too_many_targets");
        }

        let mut seen_recipients = HashSet::new();
        let mut seen_terminals = HashSet::new();
        let mut total_payload_bytes = 0_usize;
        let mut targets = Vec::with_capacity(raw_targets.len());
        for raw_target in raw_targets {
            let Some(target) = raw_target.as_object() else {
                return err("invalid_target");
            };
            let recipient_id = s(target, "recipient_id");
            let host_terminal_id = s(target, "host_terminal_id");
            if recipient_id.is_empty() || host_terminal_id.is_empty() {
                return err("invalid_target");
            }
            if !seen_recipients.insert(recipient_id.clone())
                || !seen_terminals.insert(host_terminal_id.clone())
            {
                return err("duplicate_target");
            }
            let (operations, payload_bytes) = match parse_batch_operations(target) {
                Ok(parsed) => parsed,
                Err(code) => {
                    targets.push(Err(json!({
                        "recipient_id": recipient_id,
                        "host_terminal_id": host_terminal_id,
                        "ok": false,
                        "error": code,
                        "stage": "none",
                    })));
                    continue;
                }
            };
            total_payload_bytes = match total_payload_bytes.checked_add(payload_bytes) {
                Some(total) if total <= MAX_WRITE_BYTES => total,
                _ => return err("request_too_large"),
            };
            targets.push(Ok(BatchTarget {
                recipient_id,
                host_terminal_id,
                operations,
            }));
        }
        let inner = self.inner.lock().await;
        let mut results = Vec::with_capacity(targets.len());
        let mut scheduled = Vec::new();
        for (target_index, target) in targets.into_iter().enumerate() {
            let target = match target {
                Ok(target) => target,
                Err(result) => {
                    results.push(result);
                    continue;
                }
            };
            let Some(identity) = inner.by_host_id.get(&target.host_terminal_id) else {
                results.push(batch_error(&target, "not_found"));
                continue;
            };
            if !inner.terminals.contains_key(identity) {
                results.push(batch_error(&target, "not_found"));
                continue;
            }
            let mut due_ms = 0_u64;
            for (operation_index, operation) in target.operations.into_iter().enumerate() {
                due_ms += operation.delay_ms;
                scheduled.push(ScheduledOperation {
                    due_ms,
                    target_index,
                    operation_index,
                    host_terminal_id: target.host_terminal_id.clone(),
                    payload: operation.payload,
                });
            }
            results.push(json!({
                "recipient_id": target.recipient_id,
                "host_terminal_id": target.host_terminal_id,
                "ok": true,
                "written": true,
            }));
        }
        drop(inner);
        scheduled.sort_by_key(|operation| {
            (
                operation.due_ms,
                operation.target_index,
                operation.operation_index,
            )
        });
        let started = Instant::now();
        for operation in scheduled {
            if results[operation.target_index]
                .get("ok")
                .and_then(Value::as_bool)
                != Some(true)
            {
                continue;
            }
            sleep_until(started + Duration::from_millis(operation.due_ms)).await;
            let inner = self.inner.lock().await;
            let Some(identity) = inner.by_host_id.get(&operation.host_terminal_id) else {
                mark_batch_failure(
                    &mut results[operation.target_index],
                    "not_found",
                    operation.operation_index,
                );
                continue;
            };
            let Some(slot) = inner.terminals.get(identity) else {
                mark_batch_failure(
                    &mut results[operation.target_index],
                    "not_found",
                    operation.operation_index,
                );
                continue;
            };
            #[cfg(feature = "vt-engine")]
            match slot.child.as_ref() {
                Some(child) => {
                    if child
                        .runtime
                        .try_send_bytes(bytes::Bytes::from(operation.payload))
                        .is_err()
                    {
                        mark_batch_failure(
                            &mut results[operation.target_index],
                            "write_queue_unavailable",
                            operation.operation_index,
                        );
                    }
                }
                None => mark_batch_failure(
                    &mut results[operation.target_index],
                    "terminal_not_running",
                    operation.operation_index,
                ),
            }
            #[cfg(not(feature = "vt-engine"))]
            let _ = (slot, operation.payload);
        }
        json!({"ok": true, "results": results})
    }
}

/// Bytes or paste text bound for a native PTY, shared by the control `write`
/// verb and the granted frame stream.
pub(crate) enum NativeInput {
    Bytes(Vec<u8>),
    Paste(String),
}

impl NativeInput {
    fn len(&self) -> usize {
        match self {
            Self::Bytes(bytes) => bytes.len(),
            Self::Paste(text) => text.len(),
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            Self::Bytes(_) => "input",
            Self::Paste(_) => "paste",
        }
    }

    /// `esc`/`ctrl_c` only when the whole `Input` payload is that one byte,
    /// mirroring `is_interrupt_input` in the daemon's turn observer.
    fn interrupt(&self) -> Option<&'static str> {
        match self {
            Self::Bytes(bytes) if bytes.as_slice() == b"\x1b" => Some("esc"),
            Self::Bytes(bytes) if bytes.as_slice() == b"\x03" => Some("ctrl_c"),
            _ => None,
        }
    }
}

/// Resolves a native slot by host id: `not_found` for unknown ids and
/// `not_native` for tmux panes.
fn native_slot_mut<'a>(
    inner: &'a mut Inner,
    host_terminal_id: &str,
) -> Result<&'a mut TerminalSlot, &'static str> {
    let identity = inner
        .by_host_id
        .get(host_terminal_id)
        .cloned()
        .ok_or("not_found")?;
    let slot = inner.terminals.get_mut(&identity).ok_or("not_found")?;
    if slot.locator.is_some() {
        return Err("not_native");
    }
    Ok(slot)
}

/// Hands `input` to a native slot's PTY. The size cap and the vt-engine gate
/// live here so the control verb and the frame stream refuse identically:
/// `request_too_large` over `MAX_WRITE_BYTES`, `pty_busy` when the bounded
/// writer is full, `terminal_gone` once the writer has closed.
///
/// Every path out of here either handed the bytes to a writer or names why it
/// could not. `Ok(())` means delivered, and the frame stream emits
/// `input_activity` on it, so a path answering success for input it dropped
/// would credit a keystroke that reached no terminal.
fn deliver_native(slot: &TerminalSlot, input: NativeInput) -> Result<(), &'static str> {
    if input.len() > MAX_WRITE_BYTES {
        return Err("request_too_large");
    }
    send_to_pty(slot, input)
}

#[cfg(feature = "vt-engine")]
fn send_to_pty(slot: &TerminalSlot, input: NativeInput) -> Result<(), &'static str> {
    use tokio::sync::mpsc::error::TrySendError;

    // A slot outlives its child, so no writer means the PTY this input was aimed
    // at has already gone.
    let child = slot.child.as_ref().ok_or("terminal_gone")?;
    let sent = match input {
        NativeInput::Bytes(bytes) => child.runtime.try_send_bytes(bytes::Bytes::from(bytes)),
        NativeInput::Paste(text) => child.runtime.try_send_paste(text),
    };
    match sent {
        Ok(()) => Ok(()),
        Err(TrySendError::Full(_)) => Err("pty_busy"),
        Err(TrySendError::Closed(_)) => Err("terminal_gone"),
    }
}

/// This build links no VT engine, so no slot owns a PTY writer and nothing can
/// reach a terminal. `native_slot_alive` answers `false` here for the same
/// reason.
#[cfg(not(feature = "vt-engine"))]
fn send_to_pty(_slot: &TerminalSlot, _input: NativeInput) -> Result<(), &'static str> {
    Err("terminal_gone")
}

fn parse_batch_operations(
    target: &Map<String, Value>,
) -> Result<(Vec<BatchOperation>, usize), &'static str> {
    let operations = target
        .get("operations")
        .and_then(Value::as_array)
        .ok_or("invalid_operations")?;
    if operations.is_empty() {
        return Err("invalid_operations");
    }
    if operations.len() > MAX_WRITE_BATCH_OPERATIONS_PER_TARGET {
        return Err("too_many_operations");
    }
    let mut parsed = Vec::with_capacity(operations.len());
    let mut total_delay_ms = 0_u64;
    let mut payload_bytes = 0_usize;
    for operation in operations {
        let operation = operation.as_object().ok_or("invalid_operation")?;
        let kind = operation
            .get("kind")
            .and_then(Value::as_str)
            .ok_or("invalid_operation")?;
        if !matches!(kind, "text" | "key") {
            return Err("invalid_kind");
        }
        if operation.get("encoding").and_then(Value::as_str) != Some("utf8-b64") {
            return Err("invalid_encoding");
        }
        let data = operation
            .get("data")
            .and_then(Value::as_str)
            .ok_or("invalid_encoding")?;
        let raw = base64::engine::general_purpose::STANDARD
            .decode(data.as_bytes())
            .map_err(|_| "invalid_encoding")?;
        payload_bytes = payload_bytes
            .checked_add(raw.len())
            .ok_or("request_too_large")?;
        let delay_ms = operation
            .get("delay_ms")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        if delay_ms > MAX_WRITE_BATCH_DELAY_MS {
            return Err("invalid_delay");
        }
        total_delay_ms = total_delay_ms
            .checked_add(delay_ms)
            .ok_or("invalid_delay")?;
        if total_delay_ms > MAX_WRITE_BATCH_TOTAL_DELAY_MS {
            return Err("invalid_delay");
        }
        let payload = if kind == "key" {
            named_key_bytes(&String::from_utf8_lossy(&raw))
        } else {
            raw
        };
        parsed.push(BatchOperation { delay_ms, payload });
    }
    Ok((parsed, payload_bytes))
}

fn mark_batch_failure(result: &mut Value, code: &'static str, operation_index: usize) {
    let Some(result) = result.as_object_mut() else {
        return;
    };
    result.insert("ok".to_string(), Value::Bool(false));
    result.insert("written".to_string(), Value::Bool(false));
    result.insert("error".to_string(), Value::String(code.to_string()));
    result.insert(
        "stage".to_string(),
        Value::String(
            if operation_index == 0 {
                "none"
            } else {
                "partial"
            }
            .to_string(),
        ),
    );
}

fn batch_error(target: &BatchTarget, code: &'static str) -> Value {
    json!({
        "recipient_id": target.recipient_id,
        "host_terminal_id": target.host_terminal_id,
        "ok": false,
        "error": code,
        "stage": "none",
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::host::config::HostConfig;
    use crate::protocol::RenderEncoding;
    use tokio::sync::watch;
    use tokio::time::timeout;

    fn state() -> std::sync::Arc<HostState> {
        let (shutdown, _) = watch::channel(false);
        HostState::new(
            HostConfig::default(),
            "control".to_string(),
            "local".to_string(),
            "epoch".to_string(),
            "version".to_string(),
            1,
            shutdown,
        )
    }

    fn target(recipient_id: &str, terminal_id: &str) -> Value {
        json!({
            "recipient_id": recipient_id,
            "host_terminal_id": terminal_id,
            "operations": [
                {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0},
            ],
        })
    }

    async fn remove_terminal(state: &HostState, host_terminal_id: &str) {
        let mut inner = state.inner.lock().await;
        let identity = inner
            .by_host_id
            .remove(host_terminal_id)
            .expect("terminal identity");
        inner.terminals.remove(&identity).expect("terminal slot");
    }

    #[tokio::test]
    async fn batch_returns_one_ordered_result_per_target() {
        let value = json!({
            "targets": [target("r1", "t1"), target("r2", "t2"), target("r3", "t3")],
        });
        let response = state().write_batch(value.as_object().unwrap()).await;

        assert_eq!(response["ok"], true);
        assert_eq!(response["results"].as_array().unwrap().len(), 3);
        assert_eq!(response["results"][0]["recipient_id"], "r1");
        assert_eq!(response["results"][1]["recipient_id"], "r2");
        assert_eq!(response["results"][2]["recipient_id"], "r3");
        assert!(response["results"]
            .as_array()
            .unwrap()
            .iter()
            .all(|result| result["error"] == "not_found" && result["stage"] == "none"));
    }

    #[tokio::test]
    async fn batch_rejects_global_and_per_target_limits_before_writes() {
        let targets: Vec<_> = (0..=MAX_WRITE_BATCH_TARGETS)
            .map(|index| target(&format!("r{index}"), &format!("t{index}")))
            .collect();
        let too_many = json!({"targets": targets});
        let response = state().write_batch(too_many.as_object().unwrap()).await;
        assert_eq!(response["error"], "too_many_targets");

        let operations: Vec<_> = (0..=MAX_WRITE_BATCH_OPERATIONS_PER_TARGET)
            .map(|_| json!({"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0}))
            .collect();
        let invalid_target = json!({
            "targets": [{
                "recipient_id": "r1",
                "host_terminal_id": "t1",
                "operations": operations,
            }],
        });
        let response = state()
            .write_batch(invalid_target.as_object().unwrap())
            .await;
        assert_eq!(response["ok"], true);
        assert_eq!(response["results"][0]["error"], "too_many_operations");
        assert_eq!(response["results"][0]["stage"], "none");

        let delayed = json!({
            "targets": [{
                "recipient_id": "r1",
                "host_terminal_id": "t1",
                "operations": (0..6).map(|_| json!({
                    "kind": "text",
                    "encoding": "utf8-b64",
                    "data": "eA==",
                    "delay_ms": 1_000,
                })).collect::<Vec<_>>(),
            }],
        });
        let response = state().write_batch(delayed.as_object().unwrap()).await;
        assert_eq!(response["results"][0]["error"], "invalid_delay");
        assert_eq!(response["results"][0]["stage"], "none");
    }

    #[tokio::test]
    async fn native_input_is_refused_when_the_slot_owns_no_pty() {
        let state = state();
        crate::host::state::insert_native_slot(&state, "ht-1", 24, 80).await;
        let mut inner = state.inner.lock().await;
        let slot = native_slot_mut(&mut inner, "ht-1").expect("native slot");

        // Answering Ok here would report a delivery to a terminal that owns no
        // writer, and the frame stream would emit `input_activity` for it.
        assert_eq!(
            deliver_native(slot, NativeInput::Bytes(b"x".to_vec())),
            Err("terminal_gone"),
        );
        assert_eq!(
            deliver_native(slot, NativeInput::Paste("x".to_owned())),
            Err("terminal_gone"),
        );
    }

    #[tokio::test]
    async fn batch_delay_does_not_block_input_on_another_terminal() {
        let state = state();
        crate::host::state::insert_native_slot(&state, "batch", 24, 80).await;
        crate::host::state::insert_native_slot(&state, "interactive", 24, 80).await;
        let (attachment_id, _mailbox) = state
            .attach("interactive", None, RenderEncoding::SemanticFrame, 24, 80)
            .await
            .expect("attach interactive terminal");
        state
            .bind_attachment(attachment_id, "client".to_owned())
            .await
            .expect("bind interactive terminal");

        let batch = json!({
            "targets": [{
                "recipient_id": "recipient",
                "host_terminal_id": "batch",
                "operations": [
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 1_000},
                    {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 1_000},
                ],
            }],
        });
        let batch_state = state.clone();
        let batch_task = tokio::spawn(async move {
            batch_state
                .write_batch(batch.as_object().expect("batch object"))
                .await
        });
        tokio::task::yield_now().await;

        let grant = json!({
            "host_terminal_id": "interactive",
            "attachment_id": "client",
        });
        let granted = timeout(
            Duration::from_millis(250),
            state.grant_input(grant.as_object().expect("grant object")),
        )
        .await
        .expect("grant_input blocked behind batch delay");
        assert_eq!(granted["ok"], true);

        let framed = timeout(
            Duration::from_millis(250),
            state.frame_input(Some(attachment_id), NativeInput::Bytes(b"x".to_vec())),
        )
        .await
        .expect("frame_input blocked behind batch delay");
        assert_eq!(framed, Err("terminal_gone"));

        let revoke = json!({
            "host_terminal_id": "interactive",
            "attachment_id": "client",
        });
        let revoked = timeout(
            Duration::from_millis(250),
            state.revoke_input(revoke.as_object().expect("revoke object")),
        )
        .await
        .expect("revoke_input blocked behind batch delay");
        assert_eq!(revoked["ok"], true);

        batch_task.abort();
        assert!(batch_task
            .await
            .expect_err("batch task should be cancelled")
            .is_cancelled());
    }

    #[tokio::test(start_paused = true)]
    async fn batch_reports_removed_targets_and_continues_later_operations() {
        let state = state();
        crate::host::state::insert_native_slot(&state, "removed-first", 24, 80).await;
        crate::host::state::insert_native_slot(&state, "removed-later", 24, 80).await;
        let batch = json!({
            "targets": [
                {
                    "recipient_id": "first",
                    "host_terminal_id": "removed-first",
                    "operations": [
                        {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 0},
                        {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 100},
                    ],
                },
                {
                    "recipient_id": "later",
                    "host_terminal_id": "removed-later",
                    "operations": [
                        {"kind": "text", "encoding": "utf8-b64", "data": "eA==", "delay_ms": 500},
                    ],
                },
            ],
        });
        let batch_state = state.clone();
        let batch_task = tokio::spawn(async move {
            batch_state
                .write_batch(batch.as_object().expect("batch object"))
                .await
        });
        tokio::task::yield_now().await;
        tokio::time::advance(Duration::from_millis(1)).await;
        tokio::task::yield_now().await;

        remove_terminal(&state, "removed-first").await;
        tokio::time::advance(Duration::from_millis(249)).await;
        tokio::task::yield_now().await;
        remove_terminal(&state, "removed-later").await;
        tokio::time::advance(Duration::from_millis(250)).await;

        let response = batch_task.await.expect("batch task");
        assert_eq!(response["results"][0]["error"], "not_found");
        assert_eq!(response["results"][0]["stage"], "partial");
        assert_eq!(response["results"][1]["error"], "not_found");
        assert_eq!(response["results"][1]["stage"], "none");
    }
}
