//! Create-or-join tmux observers inside `AttachTerminal`.

use std::collections::HashSet;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::mpsc;

use super::poll::{
    capture_to_frame, classify_poll, geometry_oversize, numeric_format, parse_poll_batch,
    truncate_attach_history, PollClass, STDOUT_CAP,
};
use super::state::{Attachment, CommitState, HostState, Identity, ObserverBind, TerminalSlot};
use crate::protocol::{
    CursorState, ObservationReason, ObservationState, PaneLocator, RenderEncoding, ServerMessage,
    TmuxClientIdentity, DELTA_QUEUE_ENTRIES, MAX_FRAME_SIZE,
};

pub struct AttachOutcome {
    pub attachment_id: u64,
    pub host_terminal_id: String,
    pub created: bool,
    pub rx: mpsc::Receiver<ServerMessage>,
}

pub async fn attach_frame(
    state: &Arc<HostState>,
    host_terminal_id: &str,
    reservation_id: Option<String>,
    locator: Option<PaneLocator>,
    identity: Option<TmuxClientIdentity>,
    encoding: RenderEncoding,
    rows: u16,
    cols: u16,
) -> Result<AttachOutcome, &'static str> {
    if let Some(locator) = locator {
        if reservation_id.is_some() {
            return Err("invalid_reservation");
        }
        if identity
            .as_ref()
            .is_some_and(|id| locator.matches_identity(id))
        {
            return Err("self_view");
        }
        return attach_tmux(state, locator, encoding, rows, cols).await;
    }
    let (id, rx) = state
        .attach(host_terminal_id, reservation_id, encoding, rows, cols)
        .await?;
    Ok(AttachOutcome {
        attachment_id: id,
        host_terminal_id: host_terminal_id.to_string(),
        created: false,
        rx,
    })
}

pub async fn detach_frame(state: &Arc<HostState>, attachment_id: u64) {
    let locator_key = {
        let inner = state.inner.lock().await;
        inner
            .attachments
            .get(&attachment_id)
            .and_then(|att| inner.by_host_id.get(&att.host_terminal_id))
            .and_then(|id| inner.terminals.get(id))
            .and_then(|slot| slot.locator.as_ref().map(PaneLocator::locator_key))
    };
    state.detach(attachment_id).await;
    if let Some(key) = locator_key {
        let reap = {
            let inner = state.inner.lock().await;
            inner
                .terminals
                .values()
                .find(|slot| {
                    slot.locator
                        .as_ref()
                        .is_some_and(|l| l.locator_key() == key)
                })
                .is_none_or(|slot| slot.user_attachments.is_empty())
        };
        if reap {
            reap_observer(state, &key).await;
        }
    }
}

async fn attach_tmux(
    state: &Arc<HostState>,
    locator: PaneLocator,
    encoding: RenderEncoding,
    rows: u16,
    cols: u16,
) -> Result<AttachOutcome, &'static str> {
    let key = locator.locator_key();
    let mut inner = state.inner.lock().await;
    let created = inner
        .terminals
        .values()
        .find(|slot| {
            slot.locator
                .as_ref()
                .is_some_and(|l| l.locator_key() == key)
        })
        .is_none();
    if created {
        let tmux_count = inner
            .terminals
            .values()
            .filter(|slot| slot.locator.is_some())
            .count() as u32;
        if tmux_count >= state.config.max_attached_terminals {
            return Err("capacity");
        }
        let host_terminal_id = format!("ht-{}", inner.next_host_id);
        inner.next_host_id += 1;
        let identity = Identity {
            terminal_id: key.clone(),
            spawn_key: key.clone(),
        };
        let slot = TerminalSlot {
            identity: identity.clone(),
            host_terminal_id: host_terminal_id.clone(),
            commit_state: CommitState::Committed,
            pgid: 0,
            start_time: locator.server_start_time as f64,
            title: String::new(),
            rows,
            cols,
            last_seq: 0,
            observation_state: ObservationState::Live,
            observation_reason: None,
            observation_generation: 1,
            fingerprint: 0,
            reservation_id: String::new(),
            reserve_key: String::new(),
            reserve_generation: 0,
            observer_bind: ObserverBind::None,
            commit_deadline: None,
            #[cfg(feature = "vt-engine")]
            child: None,
            written_bytes: 0,
            dropped_bytes: 0,
            total_bytes: 0,
            truncated: false,
            user_attachments: HashSet::new(),
            locator: Some(locator.clone()),
            tmux_history_bytes: 0,
            history: None,
            last_frame: None,
            observer_generation: 1,
            consecutive_failures: 0,
        };
        inner.by_host_id.insert(host_terminal_id, identity.clone());
        inner.terminals.insert(identity, slot);
    }
    let host_id = existing_host_id(&inner, &key).ok_or("not_found")?;
    let identity = inner.by_host_id.get(&host_id).cloned().ok_or("not_found")?;
    if inner
        .terminals
        .get(&identity)
        .map(|s| s.user_attachments.len() as u32)
        .unwrap_or(0)
        >= state.config.max_attachments_per_terminal
    {
        return Err("capacity");
    }
    let (tx, rx) = mpsc::channel(DELTA_QUEUE_ENTRIES);
    let id = inner.next_attachment;
    inner.next_attachment += 1;
    inner.attachments.insert(
        id,
        Attachment {
            id,
            host_terminal_id: host_id.clone(),
            encoding,
            rows,
            cols,
            scroll: 0,
            reservation_id: None,
            tx: tx.clone(),
            last_send: std::time::Instant::now(),
            desynced: true,
            delta_len: 0,
            delta_bytes: 0,
            encoder: crate::protocol::render_ansi::BlitEncoder::new(),
        },
    );
    let mut replay = None;
    if let Some(slot) = inner.terminals.get_mut(&identity) {
        slot.user_attachments.insert(id);
        if let Some(history) = slot.history.clone() {
            let _ = tx.try_send(history);
        }
        replay = slot.last_frame.clone().map(|frame| (frame, slot.last_seq));
    }
    if let Some((frame, seq)) = replay {
        if let Some(att) = inner.attachments.get_mut(&id) {
            match encoding {
                RenderEncoding::SemanticFrame => {
                    let _ = att.tx.try_send(ServerMessage::Frame(frame));
                }
                RenderEncoding::TerminalAnsi => {
                    super::helpers::push_terminal_ansi(att, &frame, seq);
                }
            }
        }
    }
    drop(inner);
    if created {
        capture_history(state, &key).await;
        start_poll(state, key.clone()).await;
        let inner = state.inner.lock().await;
        if let Some(slot) = inner.terminals.values().find(|slot| {
            slot.locator
                .as_ref()
                .is_some_and(|l| l.locator_key() == key)
        }) {
            if let Some(history) = slot.history.clone() {
                let _ = tx.try_send(history);
            }
        }
    }
    Ok(AttachOutcome {
        attachment_id: id,
        host_terminal_id: host_id,
        created,
        rx,
    })
}

fn existing_host_id(inner: &super::state::Inner, key: &str) -> Option<String> {
    inner
        .terminals
        .values()
        .find(|slot| {
            slot.locator
                .as_ref()
                .is_some_and(|l| l.locator_key() == key)
        })
        .map(|slot| slot.host_terminal_id.clone())
}

async fn reap_observer(state: &Arc<HostState>, key: &str) {
    if let Some(handle) = state.polls.lock().await.remove(key) {
        handle.abort();
    }
    let mut inner = state.inner.lock().await;
    let identity = inner
        .terminals
        .iter()
        .find(|(_, slot)| {
            slot.locator
                .as_ref()
                .is_some_and(|l| l.locator_key() == key)
        })
        .map(|(id, _)| id.clone());
    if let Some(identity) = identity {
        if let Some(slot) = inner.terminals.remove(&identity) {
            inner.by_host_id.remove(&slot.host_terminal_id);
            for att_id in slot.user_attachments {
                inner.attachments.remove(&att_id);
            }
        }
    }
}

async fn capture_history(state: &Arc<HostState>, key: &str) {
    let locator = {
        let inner = state.inner.lock().await;
        inner
            .terminals
            .values()
            .find(|slot| {
                slot.locator
                    .as_ref()
                    .is_some_and(|l| l.locator_key() == key)
            })
            .and_then(|slot| slot.locator.clone())
    };
    let Some(locator) = locator else {
        return;
    };
    let lines = state.config.tmux_attach_history_lines.max(1);
    let max_bytes = state.config.tmux_attach_history_max_bytes as usize;
    let output = tokio::task::spawn_blocking(move || {
        run_tmux(
            &locator.socket_path,
            &[
                "capture-pane",
                "-p",
                "-e",
                "-J",
                "-S",
                &format!("-{lines}"),
                "-t",
                &locator.pane_id,
            ],
        )
    })
    .await
    .ok();
    let Some(Ok(raw)) = output else {
        return;
    };
    let (text, truncated, dropped, total) =
        truncate_attach_history(&raw, max_bytes, lines as usize);
    let history = ServerMessage::AttachHistory {
        text,
        truncated,
        dropped_bytes: dropped,
        total_bytes: total,
    };
    let bytes = match &history {
        ServerMessage::AttachHistory { text, .. } => text.len() as u64,
        _ => 0,
    };
    let _ = MAX_FRAME_SIZE;
    let mut inner = state.inner.lock().await;
    if let Some(slot) = inner.terminals.values_mut().find(|slot| {
        slot.locator
            .as_ref()
            .is_some_and(|l| l.locator_key() == key)
    }) {
        slot.history = Some(history);
        slot.tmux_history_bytes = bytes;
    }
}

async fn start_poll(state: &Arc<HostState>, key: String) {
    let host = Arc::clone(state);
    let stored = key.clone();
    let handle = tokio::spawn(async move {
        poll_loop(host, key).await;
    });
    state.polls.lock().await.insert(stored, handle);
}

async fn poll_loop(state: Arc<HostState>, key: String) {
    let interval = Duration::from_millis(u64::from(state.config.tmux_poll_interval_ms.max(50)));
    let ceiling = Duration::from_millis(u64::from(
        state.config.tmux_poll_backoff_ceiling_ms.max(150),
    ));
    let mut backoff = interval;
    loop {
        tokio::time::sleep(backoff).await;
        let locator = {
            let inner = state.inner.lock().await;
            inner
                .terminals
                .values()
                .find(|slot| {
                    slot.locator
                        .as_ref()
                        .is_some_and(|l| l.locator_key() == key)
                })
                .and_then(|slot| slot.locator.clone())
        };
        let Some(locator) = locator else {
            break;
        };
        match poll_once(&locator, interval).await {
            Ok(parsed) => {
                backoff = interval;
                if parsed.pane_dead
                    || parsed.pid != locator.server_pid
                    || parsed.start_time != locator.server_start_time
                {
                    emit_exit(&state, &key).await;
                    reap_observer(&state, &key).await;
                    break;
                }
                if geometry_oversize(parsed.width, parsed.height) {
                    mark_observation(
                        &state,
                        &key,
                        ObservationState::Stale,
                        Some(ObservationReason::GeometryExceedsMaxCells),
                        true,
                    )
                    .await;
                    emit_stale(&state, &key).await;
                    continue;
                }
                let modes = parsed.modes.clone();
                let cursor = CursorState {
                    x: parsed.cursor_x,
                    y: parsed.cursor_y,
                    visible: modes.cursor_visible,
                    shape: modes.cursor_shape,
                };
                let frame = capture_to_frame(
                    &parsed.capture,
                    parsed.width,
                    parsed.height,
                    modes.clone(),
                    cursor,
                );
                if modes.pane_in_mode {
                    emit_copy_mode(&state, &key).await;
                }
                publish_frame(&state, &key, frame, parsed.title, modes.pane_in_mode).await;
            }
            Err(class) => {
                if class == PollClass::ConfirmedAbsence {
                    emit_exit(&state, &key).await;
                    reap_observer(&state, &key).await;
                    break;
                }
                let reason = class.reason();
                mark_observation(&state, &key, ObservationState::Stale, reason, true).await;
                emit_stale(&state, &key).await;
                backoff = (backoff * 2).min(ceiling);
                let failures = {
                    let inner = state.inner.lock().await;
                    inner
                        .terminals
                        .values()
                        .find(|slot| {
                            slot.locator
                                .as_ref()
                                .is_some_and(|l| l.locator_key() == key)
                        })
                        .map(|slot| slot.consecutive_failures)
                        .unwrap_or(0)
                };
                if backoff >= ceiling && failures >= 3 {
                    mark_observation(
                        &state,
                        &key,
                        ObservationState::OrphanedObservation,
                        Some(ObservationReason::ObservationCeiling),
                        true,
                    )
                    .await;
                }
            }
        }
    }
}

async fn poll_once(
    locator: &PaneLocator,
    timeout: Duration,
) -> Result<super::poll::ParsedPoll, PollClass> {
    let socket = locator.socket_path.clone();
    let pane = locator.pane_id.clone();
    let format = numeric_format().to_string();
    let result = tokio::time::timeout(
        timeout.saturating_mul(4).max(Duration::from_millis(400)),
        tokio::task::spawn_blocking(move || run_tmux_batch(&socket, &pane, &format)),
    )
    .await;
    match result {
        Err(_) => Err(PollClass::Timeout),
        Ok(Err(_)) => Err(PollClass::SpawnFailed),
        Ok(Ok(Err(class))) => Err(class),
        Ok(Ok(Ok(stdout))) => {
            if stdout.len() > STDOUT_CAP {
                return Err(PollClass::Unparseable);
            }
            parse_poll_batch(&stdout).ok_or(PollClass::Unparseable)
        }
    }
}

fn run_tmux_batch(socket: &str, pane: &str, format: &str) -> Result<String, PollClass> {
    let title_fmt = "GTERM_TITLE_LEN=#{n:pane_title}\nGTERM_TITLE=#{pane_title}";
    let output = tmux_command()
        .args([
            "-S",
            socket,
            "display-message",
            "-p",
            "-t",
            pane,
            format,
            ";",
            "display-message",
            "-p",
            "-t",
            pane,
            title_fmt,
            ";",
            "capture-pane",
            "-p",
            "-e",
            "-N",
            "-t",
            pane,
        ])
        .output();
    match output {
        Err(err) => Err(classify_poll(
            None,
            &err.to_string(),
            "",
            Some(err.kind()),
            None,
        )),
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
            let stderr = String::from_utf8_lossy(&out.stderr).into_owned();
            let class = classify_poll(
                Some(out.status.code().unwrap_or(1)),
                &stderr,
                &stdout,
                None,
                None,
            );
            if class == PollClass::Live || parse_poll_batch(&stdout).is_some() {
                Ok(stdout)
            } else {
                Err(class)
            }
        }
    }
}

fn run_tmux(socket: &str, args: &[&str]) -> Result<String, PollClass> {
    let output = tmux_command().args(["-S", socket]).args(args).output();
    match output {
        Err(err) => Err(classify_poll(
            None,
            &err.to_string(),
            "",
            Some(err.kind()),
            None,
        )),
        Ok(out) if out.status.success() => Ok(String::from_utf8_lossy(&out.stdout).into_owned()),
        Ok(out) => Err(classify_poll(
            Some(out.status.code().unwrap_or(1)),
            &String::from_utf8_lossy(&out.stderr),
            &String::from_utf8_lossy(&out.stdout),
            None,
            None,
        )),
    }
}

fn tmux_command() -> Command {
    Command::new(std::env::var("GTERM_TMUX_BIN").unwrap_or_else(|_| "tmux".into()))
}

async fn publish_frame(
    state: &Arc<HostState>,
    key: &str,
    frame: crate::protocol::FrameData,
    title: String,
    copy_mode: bool,
) {
    let mut inner = state.inner.lock().await;
    let (seq, ids) = {
        let Some(slot) = inner.terminals.values_mut().find(|slot| {
            slot.locator
                .as_ref()
                .is_some_and(|l| l.locator_key() == key)
        }) else {
            return;
        };
        slot.rows = frame.height;
        slot.cols = frame.width;
        slot.title = super::helpers::truncate_title(&title);
        slot.last_seq += 1;
        slot.observation_state = ObservationState::Live;
        slot.observation_reason = None;
        slot.consecutive_failures = 0;
        slot.last_frame = Some(frame.clone());
        (
            slot.last_seq,
            slot.user_attachments.iter().copied().collect::<Vec<_>>(),
        )
    };
    for id in ids {
        if let Some(att) = inner.attachments.get_mut(&id) {
            let msg = if copy_mode {
                ServerMessage::Error {
                    code: "copy_mode".into(),
                    message: None,
                }
            } else {
                match att.encoding {
                    RenderEncoding::SemanticFrame => ServerMessage::Frame(frame.clone()),
                    RenderEncoding::TerminalAnsi => {
                        super::helpers::push_terminal_ansi(att, &frame, seq);
                        continue;
                    }
                }
            };
            if att.tx.try_send(msg).is_ok() {
                att.desynced = false;
                att.last_send = std::time::Instant::now();
            }
        }
    }
}

async fn emit_stale(state: &Arc<HostState>, key: &str) {
    emit_code(state, key, "stale").await;
}

async fn emit_copy_mode(state: &Arc<HostState>, key: &str) {
    emit_code(state, key, "copy_mode").await;
}

async fn emit_code(state: &Arc<HostState>, key: &str, code: &str) {
    let inner = state.inner.lock().await;
    let Some(slot) = inner.terminals.values().find(|slot| {
        slot.locator
            .as_ref()
            .is_some_and(|l| l.locator_key() == key)
    }) else {
        return;
    };
    let ids: Vec<u64> = slot.user_attachments.iter().copied().collect();
    for id in ids {
        if let Some(att) = inner.attachments.get(&id) {
            let _ = att.tx.try_send(ServerMessage::Error {
                code: code.into(),
                message: None,
            });
        }
    }
}

async fn emit_exit(state: &Arc<HostState>, key: &str) {
    let inner = state.inner.lock().await;
    let Some(slot) = inner.terminals.values().find(|slot| {
        slot.locator
            .as_ref()
            .is_some_and(|l| l.locator_key() == key)
    }) else {
        return;
    };
    let host_id = slot.host_terminal_id.clone();
    let ids: Vec<u64> = slot.user_attachments.iter().copied().collect();
    for id in ids {
        if let Some(att) = inner.attachments.get(&id) {
            let _ = att.tx.try_send(ServerMessage::TerminalExited {
                host_terminal_id: host_id.clone(),
                exit_code: None,
            });
        }
    }
}

async fn mark_observation(
    state: &Arc<HostState>,
    key: &str,
    observation_state: ObservationState,
    reason: Option<ObservationReason>,
    bump_failure: bool,
) {
    let mut inner = state.inner.lock().await;
    if let Some(slot) = inner.terminals.values_mut().find(|slot| {
        slot.locator
            .as_ref()
            .is_some_and(|l| l.locator_key() == key)
    }) {
        slot.observation_state = observation_state;
        slot.observation_reason = reason;
        slot.observation_generation = slot.observation_generation.saturating_add(1);
        if bump_failure {
            slot.consecutive_failures = slot.consecutive_failures.saturating_add(1);
        }
    }
}
