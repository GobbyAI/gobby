//! The Native write runtime: the coordinator's [`Runtime`] and
//! [`BatchRuntime`] over an authenticated host connection of its own. It
//! mirrors `NativeTerminalRuntime._write` and `write_batch` in
//! `src/gobby/terminals/native_runtime.py`.

use std::time::Duration;

use serde_json::Value;
use tokio::net::UnixStream;
use tokio::sync::Mutex;

use crate::control::{
    ControlClient, ControlError, WriteBatchOp, WriteBatchTarget, WriteKind, WriteRequest,
};
use crate::host::{HostFailure, HostOptions, open_control};
use crate::repository::Terminal;
use crate::write::{
    BatchResult, BatchRuntime, BatchTarget, BoxFuture, Delivery, Kind, Runtime, Stage, Step,
    WriteFailure,
};

/// `TMUX_TEXT_ENTER_DELAY_SECONDS` (`agents/tmux/text_injection.py`): the
/// pause between a submitted text and its Enter.
const ENTER_DELAY: Duration = Duration::from_secs(1);

/// Ledger refusals after which `ControlClient` must be discarded.
const DISCARDING_ERRORS: &[&str] = &["operation_expired", "operation_conflict"];

struct Connection {
    client: ControlClient<UnixStream>,
    epoch: String,
}

/// Writes to Native rows through one connection, reopened after a loss.
pub struct NativeWrite {
    options: HostOptions,
    connection: Mutex<Option<Connection>>,
}

impl NativeWrite {
    pub fn new(options: HostOptions) -> Self {
        Self {
            options,
            connection: Mutex::new(None),
        }
    }

    /// The held connection, or a newly opened one.
    async fn open<'s>(
        &self,
        slot: &'s mut Option<Connection>,
    ) -> Result<&'s mut Connection, HostFailure> {
        let connection = match slot.take() {
            Some(connection) => connection,
            None => {
                let (client, info) = open_control(&self.options).await?;
                Connection {
                    client,
                    epoch: info.epoch,
                }
            }
        };
        Ok(slot.insert(connection))
    }

    /// One `write` request. A refusal fails at `stage`; a lost connection
    /// is uncertain.
    async fn send(
        &self,
        terminal: &Terminal,
        request: WriteRequest,
        stage: Stage,
    ) -> Result<Delivery, WriteFailure> {
        let mut slot = self.connection.lock().await;
        let connection = self
            .open(&mut slot)
            .await
            .map_err(|error| refused(stage, "host_unavailable", error))?;
        if terminal.host_epoch.as_deref() != Some(connection.epoch.as_str()) {
            return Err(refused(stage, "host_epoch_changed", "host epoch changed"));
        }
        let reply = connection.client.write(&request).await;
        match reply {
            Ok(_) => Ok(Delivery::Delivered),
            Err(error) => match settle(&mut slot, stage, error) {
                Ok(failure) => Err(failure),
                Err(detail) => Ok(Delivery::Uncertain { detail }),
            },
        }
    }
}

impl Runtime for NativeWrite {
    fn write<'a>(
        &'a self,
        terminal: &'a Terminal,
        step: &'a Step,
    ) -> BoxFuture<'a, Result<Delivery, WriteFailure>> {
        Box::pin(async move {
            let host_terminal_id = host_terminal_id(terminal).ok_or_else(|| {
                refused(
                    Stage::None,
                    "host_terminal_id_missing",
                    "row has no host terminal id",
                )
            })?;
            let request = |kind, data: &[u8], submit| WriteRequest {
                host_terminal_id: host_terminal_id.into(),
                kind,
                data: data.to_vec(),
                submit,
            };
            let data = step.payload.as_bytes();
            if step.kind == Kind::Text && step.submit {
                let text = request(WriteKind::Text, data, Some(false));
                let delivery = self.send(terminal, text, Stage::None).await?;
                if delivery != Delivery::Delivered {
                    return Ok(delivery);
                }
                tokio::time::sleep(ENTER_DELAY).await;
                let enter = request(WriteKind::Key, b"enter", None);
                return self.send(terminal, enter, Stage::Partial).await;
            }
            let submit = (step.kind == Kind::Text).then_some(false);
            let single = request(write_kind(step.kind), data, submit);
            self.send(terminal, single, Stage::None).await
        })
    }

    fn batch(&self) -> Option<&dyn BatchRuntime> {
        Some(self)
    }
}

impl BatchRuntime for NativeWrite {
    fn write_batch(
        &self,
        targets: Vec<BatchTarget>,
    ) -> BoxFuture<'_, Result<Vec<BatchResult>, String>> {
        Box::pin(async move {
            let mut slot = self.connection.lock().await;
            let connection = match self.open(&mut slot).await {
                Ok(connection) => connection,
                Err(error) => {
                    let failure = refused(Stage::None, "host_unavailable", error);
                    return Ok(targets
                        .into_iter()
                        .map(|target| BatchResult {
                            result_id: target.result_id,
                            outcome: Err(failure.clone()),
                        })
                        .collect());
                }
            };
            // Targets refused here never reach the host.
            let mut outcomes = Vec::with_capacity(targets.len());
            let mut sent = Vec::new();
            for target in &targets {
                let outcome = match host_terminal_id(&target.terminal) {
                    None => Some(Err(refused(
                        Stage::None,
                        "host_terminal_id_missing",
                        "row has no host terminal id",
                    ))),
                    Some(_)
                        if target.terminal.host_epoch.as_deref()
                            != Some(connection.epoch.as_str()) =>
                    {
                        Some(Err(refused(
                            Stage::None,
                            "host_epoch_changed",
                            "host epoch changed",
                        )))
                    }
                    Some(id) => {
                        sent.push(WriteBatchTarget {
                            recipient_id: target.result_id.clone(),
                            host_terminal_id: id.into(),
                            operations: target
                                .operations
                                .iter()
                                .map(|operation| WriteBatchOp {
                                    kind: write_kind(operation.kind),
                                    data: operation.payload.as_bytes().to_vec(),
                                    delay_ms: operation.delay_ms,
                                })
                                .collect(),
                        });
                        None
                    }
                };
                outcomes.push(outcome);
            }
            let fates = if sent.is_empty() {
                Vec::new()
            } else {
                let reply = connection.client.write_batch(&sent).await;
                match reply {
                    Ok(reply) => decode(&reply, &sent).unwrap_or_else(|| {
                        uncertain(&sent, "write_batch reply does not account for every target")
                    }),
                    Err(error) => match settle(&mut slot, Stage::None, error) {
                        Ok(failure) => sent.iter().map(|_| Err(failure.clone())).collect(),
                        Err(detail) => uncertain(&sent, &detail),
                    },
                }
            };
            let mut fates = fates.into_iter();
            Ok(targets
                .into_iter()
                .zip(outcomes)
                .map(|(target, outcome)| BatchResult {
                    result_id: target.result_id,
                    outcome: outcome.or_else(|| fates.next()).unwrap_or_else(|| {
                        Ok(Delivery::Uncertain {
                            detail: "write_batch result missing".into(),
                        })
                    }),
                })
                .collect())
        })
    }
}

/// The host terminal a row's locator names.
fn host_terminal_id(terminal: &Terminal) -> Option<&str> {
    terminal
        .locator
        .as_ref()?
        .get("host_terminal_id")?
        .as_str()
        .filter(|id| !id.is_empty())
}

const fn write_kind(kind: Kind) -> WriteKind {
    match kind {
        Kind::Text => WriteKind::Text,
        Kind::Key => WriteKind::Key,
        Kind::Paste => WriteKind::Paste,
        Kind::Input => WriteKind::Input,
    }
}

fn refused(stage: Stage, code: &str, detail: impl ToString) -> WriteFailure {
    WriteFailure {
        stage,
        code: code.into(),
        detail: detail.to_string(),
    }
}

/// A failed request: `Ok` is a refusal at `stage` that never reached the
/// PTY, and `Err` is a lost connection, dropped so the next request reopens,
/// whose request may have landed.
fn settle(
    slot: &mut Option<Connection>,
    stage: Stage,
    error: ControlError,
) -> Result<WriteFailure, String> {
    match error {
        ControlError::Host { error, detail, .. } => {
            if DISCARDING_ERRORS.contains(&error.as_str()) {
                *slot = None;
            }
            let detail = detail.unwrap_or_else(|| error.clone());
            Ok(refused(stage, &error, detail))
        }
        ControlError::LineTooLong => Ok(refused(stage, "request_too_large", error)),
        error => {
            *slot = None;
            Err(error.to_string())
        }
    }
}

fn uncertain(sent: &[WriteBatchTarget], detail: &str) -> Vec<Result<Delivery, WriteFailure>> {
    sent.iter()
        .map(|_| {
            Ok(Delivery::Uncertain {
                detail: detail.into(),
            })
        })
        .collect()
}

/// The host's result for each sent target in order, or `None` when the
/// reply does not account for them as `host_client.write_batch` checks.
fn decode(reply: &Value, sent: &[WriteBatchTarget]) -> Option<Vec<Result<Delivery, WriteFailure>>> {
    let results = reply.get("results")?.as_array()?;
    if results.len() != sent.len() {
        return None;
    }
    sent.iter()
        .zip(results)
        .map(|(target, item)| {
            if item.get("recipient_id")?.as_str()? != target.recipient_id
                || item.get("host_terminal_id")?.as_str()? != target.host_terminal_id
            {
                return None;
            }
            if item.get("ok")?.as_bool()? {
                return (item.get("written") == Some(&Value::Bool(true)))
                    .then_some(Ok(Delivery::Delivered));
            }
            let code = item.get("error")?.as_str()?;
            let stage = match item.get("stage")?.as_str()? {
                "none" => Stage::None,
                "partial" => Stage::Partial,
                _ => return None,
            };
            Some(Err(refused(stage, code, code)))
        })
        .collect()
}
