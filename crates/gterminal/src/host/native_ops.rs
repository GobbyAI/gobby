//! Native-terminal resource lifecycle and frame production.

use std::collections::BTreeSet;
use std::io;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

#[cfg(feature = "vt-engine")]
use super::helpers::truncate_title;
use super::helpers::{err, native_entitlements, push_semantic_frame, push_terminal_ansi, s};
#[cfg(feature = "vt-engine")]
use super::spawn::CommitResult;
use super::state::{CommitState, HostState, Identity, ObserverBind, Reservation, TerminalSlot};
use crate::protocol::{
    validate_dimensions, RenderEncoding, ServerMessage, SnapshotMode, SNAPSHOT_DEFAULT_MAX_BYTES,
    SNAPSHOT_DEFAULT_MAX_LINES,
};

#[derive(Debug)]
pub(crate) enum KillGroupError {
    InvalidPgid,
    Io(io::Error),
}

impl std::fmt::Display for KillGroupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidPgid => write!(f, "invalid process group id"),
            Self::Io(err) => write!(f, "killpg failed: {err}"),
        }
    }
}

pub(crate) fn kill_group(pgid: i32, signal: i32) -> Result<(), KillGroupError> {
    if pgid <= 0 {
        return Err(KillGroupError::InvalidPgid);
    }
    // SAFETY: positive process groups are required above; libc reports any OS error.
    if unsafe { libc::killpg(pgid, signal) } == 0 {
        Ok(())
    } else {
        Err(KillGroupError::Io(io::Error::last_os_error()))
    }
}

/// Bound on waiting for a SIGKILLed group to be reaped before reporting it alive.
const KILL_REAP_TIMEOUT: Duration = Duration::from_secs(2);

/// Whether `pgid` still has members; a permission refusal counts as alive.
fn group_alive(pgid: i32) -> bool {
    match kill_group(pgid, 0) {
        Ok(()) => true,
        Err(KillGroupError::Io(err)) => err.raw_os_error() != Some(libc::ESRCH),
        Err(KillGroupError::InvalidPgid) => false,
    }
}

async fn group_exits_by(pgid: i32, deadline: Instant) -> bool {
    loop {
        if !group_alive(pgid) {
            return true;
        }
        let now = Instant::now();
        if now >= deadline {
            return false;
        }
        tokio::time::sleep((deadline - now).min(Duration::from_millis(20))).await;
    }
}

/// SIGTERM the group, SIGKILL it after `grace`, and report whether it is gone.
async fn terminate_group(pgid: i32, grace: Duration) -> bool {
    if pgid <= 0 {
        return false;
    }
    if let Err(err) = kill_group(pgid, libc::SIGTERM) {
        tracing::debug!(%err, pgid, "kill_group SIGTERM failed");
    }
    if group_exits_by(pgid, Instant::now() + grace).await {
        return true;
    }
    if let Err(err) = kill_group(pgid, libc::SIGKILL) {
        tracing::debug!(%err, pgid, "kill_group SIGKILL failed");
    }
    group_exits_by(pgid, Instant::now() + KILL_REAP_TIMEOUT).await
}

pub(crate) fn trim_to_char_boundary(text: &str, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return text.to_string();
    }
    let mut start = text.len() - max_bytes;
    while start < text.len() && !text.is_char_boundary(start) {
        start += 1;
    }
    text[start..].to_string()
}

/// Refusal for a `mode` the snapshot verb does not implement. It names every
/// accepted value so a caller never has to guess after a typo.
fn invalid_snapshot_mode() -> Value {
    json!({
        "ok": false,
        "error": "invalid_mode",
        "valid_modes": SnapshotMode::VALID,
    })
}

/// `mode` selects which representation a snapshot returns. Omitting it means
/// plain text; anything else is refused rather than answered with the
/// representation the caller did not ask for.
fn snapshot_mode(extra: &Map<String, Value>) -> Result<SnapshotMode, Value> {
    match extra.get("mode") {
        None => Ok(SnapshotMode::Text),
        Some(Value::String(value)) => {
            SnapshotMode::from_wire(value).ok_or_else(invalid_snapshot_mode)
        }
        Some(_) => Err(invalid_snapshot_mode()),
    }
}

/// A capped snapshot body. Every counter describes the representation that was
/// selected, because the caps run after that choice.
struct SnapshotBody {
    text: String,
    truncated: bool,
    dropped_bytes: u64,
    total_bytes: u64,
}

/// The one snapshot truncation policy: keep the last `max_lines` lines, then
/// trim that tail to `max_bytes` on a UTF-8 boundary.
fn truncate_snapshot(text: &str, max_lines: usize, max_bytes: usize) -> SnapshotBody {
    let total_bytes = text.len() as u64;
    let mut truncated = false;
    let mut dropped_bytes = 0u64;
    let mut lines: Vec<&str> = text.lines().collect();
    if lines.len() > max_lines {
        dropped_bytes += lines[..lines.len() - max_lines]
            .iter()
            .map(|line| line.len() as u64 + 1)
            .sum::<u64>();
        lines = lines[lines.len() - max_lines..].to_vec();
        truncated = true;
    }
    let mut joined = lines.join("\n");
    if joined.len() > max_bytes {
        let before = joined.len();
        joined = trim_to_char_boundary(&joined, max_bytes);
        dropped_bytes += (before - joined.len()) as u64;
        truncated = true;
    }
    SnapshotBody {
        text: joined,
        truncated,
        dropped_bytes,
        total_bytes,
    }
}

#[cfg(feature = "vt-engine")]
fn commit_failure_json(outcome: CommitResult) -> Value {
    match outcome {
        CommitResult::ExecFailed(failure) => json!({
            "ok": false,
            "error": "exec_failed",
            "code": failure.code,
            "detail": failure.detail,
            "stage": failure.stage,
        }),
        CommitResult::ExecTimeout => json!({
            "ok": false,
            "error": "exec_timeout",
            "detail": "commit deadline expired",
            "stage": "execve",
        }),
        CommitResult::MalformedStatus => json!({
            "ok": false,
            "error": "malformed_status",
            "detail": "invalid exec-status JSON",
            "stage": "status",
        }),
        CommitResult::Committed => unreachable!("committed is not a commit failure"),
    }
}

fn targets_tmux(inner: &super::state::Inner, extra: &Map<String, Value>) -> bool {
    if let Some(host_id) = extra.get("host_terminal_id").and_then(Value::as_str) {
        if inner
            .by_host_id
            .get(host_id)
            .and_then(|identity| inner.terminals.get(identity))
            .is_some_and(|slot| slot.locator.is_some())
        {
            return true;
        }
    }
    if let Some(terminal_id) = extra.get("terminal_id").and_then(Value::as_str) {
        return inner.terminals.values().any(|slot| {
            slot.locator.is_some()
                && (slot.identity.terminal_id == terminal_id
                    || slot.host_terminal_id == terminal_id)
        });
    }
    false
}

fn remove_slot_attachments(inner: &mut super::state::Inner, slot: &TerminalSlot) {
    for attachment_id in &slot.user_attachments {
        if let Some(att) = inner.attachments.remove(attachment_id) {
            att.mailbox.close();
        }
    }
    if let ObserverBind::Bound { attachment_id, .. } = &slot.observer_bind {
        if let Some(att) = inner.attachments.remove(attachment_id) {
            att.mailbox.close();
        }
    }
}

fn remove_terminal_slot(
    inner: &mut super::state::Inner,
    identity: &Identity,
    kill_signal: Option<i32>,
) {
    if let Some(slot) = inner.terminals.remove(identity) {
        inner.by_host_id.remove(&slot.host_terminal_id);
        inner.reservations.remove(&slot.reservation_id);
        remove_slot_attachments(inner, &slot);
        if let Some(signal) = kill_signal {
            if let Err(err) = kill_group(slot.pgid, signal) {
                tracing::debug!(%err, pgid = slot.pgid, "kill_group failed");
            }
        }
    }
}

/// Whether a native slot still owns a live group for the shutdown drain,
/// including a group retained past its leader after an unproven kill.
#[cfg(feature = "vt-engine")]
fn native_slot_alive(slot: &TerminalSlot) -> bool {
    slot.locator.is_none()
        && slot.pgid > 0
        && (slot
            .child
            .as_ref()
            .is_some_and(|child| child.runtime.child_exit().is_none())
            || holds_live_group(slot))
}

/// Whether a slot's leader exit must not retire it: an in-flight kill owns
/// it, or an unproven kill left its group alive past the leader.
#[cfg(any(feature = "vt-engine", test))]
fn holds_live_group(slot: &TerminalSlot) -> bool {
    slot.killing || (slot.kill_unproven && group_alive(slot.pgid))
}

#[cfg(not(feature = "vt-engine"))]
fn native_slot_alive(_slot: &TerminalSlot) -> bool {
    false
}

impl HostState {
    pub fn begin_shutdown(self: &Arc<Self>, grace_ms: u64) -> Value {
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return err("host_upgrading");
        };
        if !self.draining.swap(true, Ordering::SeqCst) {
            let state = Arc::clone(self);
            tokio::spawn(async move {
                state
                    .drain_native_children(Duration::from_millis(grace_ms))
                    .await;
                let _ = state.shutdown.send(true);
            });
        }
        json!({"ok": true, "accepted": true, "draining": true})
    }

    async fn drain_native_children(&self, grace: Duration) {
        let targets: BTreeSet<i32> = {
            let inner = self.inner.lock().await;
            inner
                .terminals
                .values()
                .filter(|slot| native_slot_alive(slot))
                .map(|slot| slot.pgid)
                .collect()
        };
        for pgid in &targets {
            if let Err(err) = kill_group(*pgid, libc::SIGHUP) {
                tracing::debug!(%err, pgid = *pgid, "kill_group SIGHUP failed");
            }
        }

        let deadline = Instant::now() + grace;
        loop {
            let survivors = self.live_native_pgids(&targets).await;
            if survivors.is_empty() {
                return;
            }
            let now = Instant::now();
            if now >= deadline {
                for pgid in survivors {
                    if let Err(err) = kill_group(pgid, libc::SIGKILL) {
                        tracing::debug!(%err, pgid, "kill_group SIGKILL failed");
                    }
                }
                return;
            }
            tokio::time::sleep((deadline - now).min(Duration::from_millis(20))).await;
        }
    }

    async fn live_native_pgids(&self, targets: &BTreeSet<i32>) -> Vec<i32> {
        let inner = self.inner.lock().await;
        targets
            .iter()
            .copied()
            .filter(|pgid| {
                inner
                    .terminals
                    .values()
                    .any(|slot| slot.pgid == *pgid && native_slot_alive(slot))
            })
            .collect()
    }

    pub async fn reserve_observer(&self, conn_id: u64, extra: &Map<String, Value>) -> Value {
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return err("host_upgrading");
        };
        let terminal_id = extra
            .get("terminal_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let reserve_key = extra
            .get("reserve_key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if terminal_id.is_empty() || reserve_key.is_empty() {
            return err("invalid_request");
        }
        let mut inner = self.inner.lock().await;
        if targets_tmux(&inner, extra) {
            return err("not_native");
        }
        if let Some(existing) = inner.reservations.values().find(|res| {
            res.conn_id == conn_id && res.terminal_id == terminal_id && res.key == reserve_key
        }) {
            return json!({
                "ok": true,
                "reservation_id": existing.id,
                "reserve_key": existing.key,
                "reserve_generation": existing.generation,
            });
        }
        if native_entitlements(&inner) >= self.config.native_entitlement_ceiling() {
            return err("capacity");
        }
        let reservation_id = format!("rsv-{}", uuid::Uuid::new_v4());
        inner.reservations.insert(
            reservation_id.clone(),
            Reservation {
                id: reservation_id.clone(),
                key: reserve_key.clone(),
                generation: 1,
                terminal_id,
                conn_id,
                identity: None,
                prepared: false,
            },
        );
        json!({
            "ok": true,
            "reservation_id": reservation_id,
            "reserve_key": reserve_key,
            "reserve_generation": 1,
        })
    }

    pub async fn release_observer(&self, extra: &Map<String, Value>) -> Value {
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return err("host_upgrading");
        };
        let reservation_id = extra
            .get("reservation_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let reserve_key = extra
            .get("reserve_key")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut inner = self.inner.lock().await;
        if targets_tmux(&inner, extra) {
            return err("not_native");
        }
        if let Some(res) = inner.reservations.get(reservation_id) {
            if res.prepared || (res.key != reserve_key && !reserve_key.is_empty()) {
                return json!({"ok": true, "released": false});
            }
            inner.reservations.remove(reservation_id);
        }
        json!({"ok": true, "released": true})
    }

    pub async fn spawn_commit(self: &Arc<Self>, extra: &Map<String, Value>) -> Value {
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return err("host_upgrading");
        };
        let identity = Identity {
            terminal_id: s(extra, "terminal_id"),
            spawn_key: s(extra, "spawn_key"),
        };
        let deadline_ms = match extra.get("commit_deadline_ms") {
            None => 30_000,
            Some(value) => match value.as_u64() {
                Some(value) if value >= 1_000 => value,
                _ => return err("invalid_request"),
            },
        };
        let mut inner = self.inner.lock().await;
        let Some(slot) = inner
            .terminals
            .get_mut(&identity)
            .filter(|slot| !slot.killing)
        else {
            return err("not_found");
        };
        if slot.commit_state == CommitState::Committed {
            return json!({
                "ok": true,
                "host_terminal_id": slot.host_terminal_id,
                "commit_state": "committed",
            });
        }
        #[cfg(feature = "vt-engine")]
        let commit = slot.child.as_mut().map(|child| child.begin_commit());
        #[cfg(not(feature = "vt-engine"))]
        let _ = deadline_ms;
        drop(inner);
        #[cfg(feature = "vt-engine")]
        let outcome = match commit {
            Some(commit) => commit.finish(Duration::from_millis(deadline_ms)).await,
            None => CommitResult::Committed,
        };
        let mut inner = self.inner.lock().await;
        // A kill that began while the commit waited owns the slot: its
        // death is neither an exec nor a commit failure to reap here.
        if inner
            .terminals
            .get(&identity)
            .is_some_and(|slot| slot.killing)
        {
            return err("not_found");
        }
        #[cfg(feature = "vt-engine")]
        match outcome {
            CommitResult::Committed => {}
            failure => {
                remove_terminal_slot(&mut inner, &identity, Some(libc::SIGKILL));
                return commit_failure_json(failure);
            }
        }
        let Some(slot) = inner.terminals.get_mut(&identity) else {
            return err("not_found");
        };
        slot.commit_state = CommitState::Committed;
        slot.commit_deadline = None;
        let host_terminal_id = slot.host_terminal_id.clone();
        #[cfg(feature = "vt-engine")]
        let exit_status = slot
            .child
            .as_ref()
            .and_then(|child| child.runtime.child_exit())
            .map(|exit| (exit.exit_code, exit.signal));
        #[cfg(feature = "vt-engine")]
        let exit_watch = slot
            .child
            .as_ref()
            .and_then(|child| child.runtime.child_exit_watch());
        #[cfg(not(feature = "vt-engine"))]
        let exit_status: Option<(Option<u32>, Option<String>)> = None;
        let already_exited = exit_status.is_some();
        let response = match exit_status {
            Some((exit_code, signal)) => json!({
                "ok": true,
                "host_terminal_id": host_terminal_id,
                "commit_state": "committed",
                "terminal_state": "exited",
                "exit_code": exit_code,
                "signal": signal,
            }),
            None => json!({
                "ok": true,
                "host_terminal_id": host_terminal_id,
                "commit_state": "committed",
            }),
        };
        if already_exited {
            remove_terminal_slot(&mut inner, &identity, None);
        }
        drop(inner);
        #[cfg(feature = "vt-engine")]
        if let Some(exit_watch) = exit_watch {
            self.watch_leader_exit(identity, host_terminal_id, exit_watch);
        }
        response
    }

    /// Settles a committed pane when its leader exits: the watcher is the
    /// only path that removes a committed pane (see `settle_leader_exit`).
    #[cfg(feature = "vt-engine")]
    pub(crate) fn watch_leader_exit(
        self: &Arc<Self>,
        identity: Identity,
        host_terminal_id: String,
        exit_watch: crate::pane::ChildExitWatch,
    ) {
        let state = Arc::clone(self);
        #[cfg(unix)]
        let hold = exit_watcher_holds(self.generation, &identity.terminal_id);
        tokio::spawn(async move {
            let Some(exit) = exit_watch.wait().await else {
                return;
            };
            #[cfg(unix)]
            if hold {
                until_write_owned(&state.mutation_gate).await;
            }
            state
                .settle_leader_exit(&identity, &host_terminal_id, exit.exit_code)
                .await;
        });
    }

    /// `grace_ms` is the request's typed field, which serde never leaves in `extra`.
    pub async fn kill(
        self: &Arc<Self>,
        extra: &Map<String, Value>,
        grace_ms: Option<u64>,
    ) -> Value {
        // Owned: the proof task below outlives a dropped connection.
        let Ok(gate) = Arc::clone(&self.mutation_gate).try_read_owned() else {
            return err("host_upgrading");
        };
        let host_terminal_id = s(extra, "host_terminal_id");
        let grace_ms = grace_ms.unwrap_or(100);
        // Mark the slot in flight and keep it listed: a listing taken during
        // the grace window must not read a live group as absent. Commits and
        // reapers leave a marked slot to this kill.
        let (identity, pgid) = {
            let mut inner = self.inner.lock().await;
            let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
                return json!({"ok": true, "killed": false});
            };
            let Some(slot) = inner.terminals.get_mut(&identity) else {
                return json!({"ok": true, "killed": false});
            };
            if slot.locator.is_some() {
                return err("not_native");
            }
            if slot.killing {
                return err("kill_in_progress");
            }
            slot.killing = true;
            (identity, slot.pgid)
        };
        // Ack only once the group is proven gone, so a host that dies
        // mid-kill never acked a live child. The proof runs in its own task:
        // a dropped connection cancels this request, and the marked slot
        // must still be removed or released.
        let state = Arc::clone(self);
        let grace = Duration::from_millis(grace_ms);
        let proof = tokio::spawn(async move {
            let _gate = gate;
            let proven = terminate_group(pgid, grace).await;
            let mut inner = state.inner.lock().await;
            if proven {
                remove_terminal_slot(&mut inner, &identity, None);
            } else {
                tracing::warn!(pgid, %host_terminal_id, "kill left the process group alive");
                if let Some(slot) = inner.terminals.get_mut(&identity) {
                    slot.killing = false;
                    slot.kill_unproven = true;
                }
            }
            proven
        });
        match proof.await {
            Ok(true) => json!({"ok": true, "killed": true}),
            Ok(false) => err("kill_unproven"),
            Err(join_err) => {
                tracing::error!(%join_err, "kill proof task failed");
                err("kill_unproven")
            }
        }
    }

    pub async fn resize(&self, extra: &Map<String, Value>) -> Value {
        let Ok(_gate) = self.mutation_gate.try_read() else {
            return err("host_upgrading");
        };
        let host_terminal_id = s(extra, "host_terminal_id");
        let rows = extra.get("rows").and_then(Value::as_i64).unwrap_or(0);
        let cols = extra.get("cols").and_then(Value::as_i64).unwrap_or(0);
        let dims = match validate_dimensions(rows, cols) {
            Ok(dims) => dims,
            Err(e) => return err(e.code()),
        };
        let mut inner = self.inner.lock().await;
        let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
            return err("not_found");
        };
        let Some(slot) = inner.terminals.get_mut(&identity) else {
            return err("not_found");
        };
        if slot.locator.is_some() {
            return err("not_native");
        }
        #[cfg(feature = "vt-engine")]
        if let Some(child) = slot.child.as_ref() {
            child.runtime.resize(dims.0, dims.1, 0, 0);
        }
        slot.rows = dims.0;
        slot.cols = dims.1;
        slot.last_seq += 1;
        // A resize reflows the grid every peer last painted, so no encoder
        // baseline still matches its peer's screen: the next frame pass sends
        // each attachment one keyframe before any further delta.
        for att in inner
            .attachments
            .values_mut()
            .filter(|att| att.host_terminal_id == host_terminal_id)
        {
            att.desynced = true;
        }
        json!({"ok": true, "rows": dims.0, "cols": dims.1})
    }

    pub async fn snapshot(&self, extra: &Map<String, Value>) -> Value {
        let mode = match snapshot_mode(extra) {
            Ok(mode) => mode,
            Err(refusal) => return refusal,
        };
        let host_terminal_id = s(extra, "host_terminal_id");
        let max_bytes = extra
            .get("max_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_BYTES as u64) as usize;
        let max_lines = extra
            .get("max_lines")
            .and_then(Value::as_u64)
            .unwrap_or(SNAPSHOT_DEFAULT_MAX_LINES as u64) as usize;
        let inner = self.inner.lock().await;
        let Some(identity) = inner.by_host_id.get(&host_terminal_id).cloned() else {
            return err("not_found");
        };
        let Some(slot) = inner.terminals.get(&identity) else {
            return err("not_found");
        };
        if slot.locator.is_some() {
            return err("not_native");
        }
        #[cfg(feature = "vt-engine")]
        let text = match slot.child.as_ref() {
            Some(child) => child
                .runtime
                .snapshot_history(mode)
                .unwrap_or_else(|| child.runtime.visible_snapshot(mode)),
            None => String::new(),
        };
        #[cfg(not(feature = "vt-engine"))]
        let text = String::new();
        let body = truncate_snapshot(&text, max_lines, max_bytes);
        json!({
            "ok": true,
            "mode": mode.as_wire(),
            "text": body.text,
            "truncated": body.truncated,
            "dropped_bytes": body.dropped_bytes,
            "total_bytes": body.total_bytes,
        })
    }

    /// Removes a committed pane whose leader exited and emits its
    /// `terminal_exited`, under the `inner` lock and then the events lock.
    /// The exit watcher is the only caller, so it alone removes a committed
    /// pane for an exit. Returns `false`, doing nothing, when a kill owns
    /// the slot: one in flight, or an unproven one whose group lives on.
    #[cfg(any(feature = "vt-engine", test))]
    async fn settle_leader_exit(
        &self,
        identity: &Identity,
        host_terminal_id: &str,
        exit_code: Option<u32>,
    ) -> bool {
        let _gate = self.mutation_gate.read().await;
        let mut inner = self.inner.lock().await;
        if let Some(slot) = inner
            .terminals
            .get(identity)
            .filter(|slot| slot.host_terminal_id == host_terminal_id)
        {
            if holds_live_group(slot) {
                return false;
            }
            remove_terminal_slot(&mut inner, identity, None);
        }
        // Emitted before the `inner` lock is released: no reader sees the
        // slot gone while its event is still unwritten.
        self.events
            .emit_terminal_exited(
                identity.terminal_id.clone(),
                host_terminal_id.to_owned(),
                exit_code,
            )
            .await;
        true
    }

    pub async fn expire_prepared(&self) {
        let _gate = self.mutation_gate.read().await;
        let mut inner = self.inner.lock().await;
        let now = Instant::now();
        let expired: Vec<Identity> = inner
            .terminals
            .iter()
            .filter(|(_, slot)| {
                !slot.killing
                    && slot.commit_state == CommitState::Prepared
                    && slot.commit_deadline.is_some_and(|deadline| deadline <= now)
            })
            .map(|(identity, _)| identity.clone())
            .collect();
        for identity in expired {
            remove_terminal_slot(&mut inner, &identity, Some(libc::SIGKILL));
        }
        // An unproven kill settled its slot with its ack and kept it only for
        // the group it left alive, so its exited leader's watcher is done and
        // owes no event. Once that group is gone the slot goes too. Any other
        // exited pane belongs to its watcher, which removes it with its event.
        #[cfg(feature = "vt-engine")]
        let abandoned: Vec<Identity> = inner
            .terminals
            .iter()
            .filter(|(_, slot)| {
                slot.kill_unproven
                    && !holds_live_group(slot)
                    && slot
                        .child
                        .as_ref()
                        .is_some_and(|child| child.runtime.child_exit().is_some())
            })
            .map(|(identity, _)| identity.clone())
            .collect();
        #[cfg(feature = "vt-engine")]
        for identity in abandoned {
            remove_terminal_slot(&mut inner, &identity, None);
        }
    }

    pub async fn broadcast_frames(self: &Arc<Self>) {
        let mut inner = self.inner.lock().await;
        let cap = self.config.delta_queue_bytes as usize;
        let lag = self.lag_timeout();
        let mut lagged = Vec::new();
        let ids: Vec<u64> = inner.attachments.keys().copied().collect();
        for id in ids {
            let (host_id, rows, cols, scroll, encoding, reusable_generation) = {
                let Some(att) = inner.attachments.get(&id) else {
                    continue;
                };
                tracing::trace!(
                    attachment_id = att.id,
                    reservation_id = att.reservation_id.as_deref(),
                    "broadcast attachment"
                );
                // A lagged stream still takes the full pass so it is closed.
                let in_sync = !att.desynced && !att.mailbox.is_lagged(lag);
                (
                    att.host_terminal_id.clone(),
                    att.rows,
                    att.cols,
                    att.scroll,
                    att.encoding,
                    att.built_generation.filter(|_| in_sync),
                )
            };
            let Some(identity) = inner.by_host_id.get(&host_id).cloned() else {
                continue;
            };
            if inner
                .terminals
                .get(&identity)
                .is_some_and(|slot| slot.locator.is_some())
            {
                continue;
            }
            #[cfg(feature = "vt-engine")]
            let (frame, seq, built_generation) = {
                let Some(slot) = inner.terminals.get_mut(&identity) else {
                    continue;
                };
                tracing::trace!(
                    written_bytes = slot.written_bytes,
                    dropped_bytes = slot.dropped_bytes,
                    total_bytes = slot.total_bytes,
                    truncated = slot.truncated,
                    observer_generation = slot.observer_generation,
                    "broadcast slot counters"
                );
                let Some(child) = slot.child.as_mut() else {
                    continue;
                };
                // Read before building: a write that races the build leaves the
                // recorded generation behind, so the next pass rebuilds.
                let generation = child.runtime.content_generation();
                if reusable_generation == Some(generation) {
                    continue;
                }
                if scroll > 0 {
                    child.runtime.set_scroll_offset_from_bottom(scroll as usize);
                } else {
                    child.runtime.scroll_reset();
                }
                let frame = child.runtime.frame_data(cols, rows);
                if scroll > 0 {
                    child.runtime.scroll_reset();
                }
                let title = child.runtime.osc_title();
                if slot.title != title {
                    slot.title = truncate_title(&title);
                    slot.last_seq += 1;
                }
                (frame, slot.last_seq, Some(generation))
            };
            #[cfg(not(feature = "vt-engine"))]
            let (frame, seq, built_generation) = {
                let _ = (scroll, reusable_generation);
                (
                    crate::protocol::FrameData {
                        cells: Vec::new(),
                        width: cols,
                        height: rows,
                        cursor: None,
                        hyperlinks: Vec::new(),
                        graphics: Vec::new(),
                        modes: crate::protocol::PaneModes::default(),
                    },
                    inner
                        .terminals
                        .get(&identity)
                        .map_or(0, |slot| slot.last_seq),
                    None,
                )
            };
            if let Some(att) = inner.attachments.get_mut(&id) {
                if att.mailbox.is_lagged(lag) {
                    att.mailbox.close_with(
                        ServerMessage::Error {
                            code: "lagged".into(),
                            message: None,
                        },
                        cap,
                    );
                    lagged.push(id);
                    continue;
                }
                let sent = match encoding {
                    RenderEncoding::SemanticFrame => push_semantic_frame(att, frame, cap),
                    RenderEncoding::TerminalAnsi => push_terminal_ansi(att, &frame, seq, cap),
                };
                // Overflow and failed keyframes set `desynced`, which forces a rebuild.
                att.built_generation = built_generation;
                if sent {
                    att.delta_len = att.delta_len.saturating_add(1);
                    att.delta_bytes = att.mailbox.queued_bytes();
                }
            }
        }
        drop(inner);
        for id in lagged {
            self.detach(id).await;
        }
    }

    pub fn lag_timeout(&self) -> Duration {
        self.config.lag_timeout()
    }
}

/// Bounds a held exit watcher when no upgrade takes the write guard.
#[cfg(all(unix, feature = "vt-engine"))]
const EXIT_WATCHER_HOLD_CAP: Duration = Duration::from_secs(20);

/// Test-only (plan gterm-host-handover 1.3.9): under `GTERM_TEST_HELPER=1`,
/// `GTERM_TEST_EXIT_WATCHER_HOLD=<terminal_id>` holds that pane's exit
/// watcher between the exit and the gate until an upgrade owns the write
/// guard, so the exit settles only after it. Only a cold-start image holds:
/// the restored one inherits the environment.
#[cfg(all(unix, feature = "vt-engine"))]
fn exit_watcher_holds(generation: u64, terminal_id: &str) -> bool {
    let helper = std::env::var_os("GTERM_TEST_HELPER").is_some_and(|value| value == "1");
    helper
        && generation == 0
        && std::env::var("GTERM_TEST_EXIT_WATCHER_HOLD").is_ok_and(|pane| pane == terminal_id)
}

/// Returns once a writer owns `gate`, or after `EXIT_WATCHER_HOLD_CAP`.
#[cfg(all(unix, feature = "vt-engine"))]
async fn until_write_owned(gate: &tokio::sync::RwLock<()>) {
    let give_up = Instant::now() + EXIT_WATCHER_HOLD_CAP;
    while gate.try_read().is_ok() && Instant::now() < give_up {
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[cfg(test)]
mod tests;
