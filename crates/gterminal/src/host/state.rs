//! Host registry, reservations, and control-verb implementations.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use tokio::sync::{watch, Mutex};

use super::backpressure::FrameMailbox;
use super::config::HostConfig;
use super::events::{EventReceiver, HostEvents};
use super::helpers::{err, list_rows, s, spawn_fingerprint};
#[cfg(feature = "vt-engine")]
use super::spawn::{spawn_prepared, PreparedChild};
use crate::protocol::render_ansi::BlitEncoder;
use crate::protocol::{
    validate_dimensions, ObservationReason, ObservationState, RenderEncoding, ServerMessage,
    LIFECYCLE_RESERVED_SLOTS,
};

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Identity {
    pub terminal_id: String,
    pub spawn_key: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CommitState {
    Prepared,
    Committed,
}

#[derive(Clone, Debug)]
pub(crate) enum ObserverBind {
    None,
    Reserved {
        reservation_id: String,
        generation: u64,
    },
    Bound {
        reservation_id: String,
        generation: u64,
        attachment_id: u64,
    },
    Entitled {
        reservation_id: String,
        generation: u64,
    },
}

pub(crate) struct Reservation {
    pub(crate) id: String,
    pub(crate) key: String,
    pub(crate) generation: u64,
    pub(crate) terminal_id: String,
    pub(crate) conn_id: u64,
    pub(crate) identity: Option<Identity>,
    pub(crate) prepared: bool,
}

pub(crate) struct TerminalSlot {
    pub(crate) identity: Identity,
    pub(crate) host_terminal_id: String,
    pub(crate) commit_state: CommitState,
    pub(crate) pgid: i32,
    pub(crate) start_time: f64,
    pub(crate) title: String,
    pub(crate) rows: u16,
    pub(crate) cols: u16,
    pub(crate) last_seq: u64,
    pub(crate) observation_state: ObservationState,
    pub(crate) observation_reason: Option<ObservationReason>,
    pub(crate) observation_generation: u64,
    pub(crate) fingerprint: u64,
    pub(crate) reservation_id: String,
    pub(crate) reserve_key: String,
    pub(crate) reserve_generation: u64,
    pub(crate) observer_bind: ObserverBind,
    pub(crate) commit_deadline: Option<Instant>,
    #[cfg(feature = "vt-engine")]
    pub(crate) child: Option<PreparedChild>,
    pub(crate) written_bytes: u64,
    pub(crate) dropped_bytes: u64,
    pub(crate) total_bytes: u64,
    pub(crate) truncated: bool,
    pub(crate) user_attachments: HashSet<u64>,
    pub(crate) locator: Option<crate::protocol::PaneLocator>,
    pub(crate) tmux_history_bytes: u64,
    pub(crate) history: Option<crate::protocol::ServerMessage>,
    pub(crate) last_frame: Option<crate::protocol::FrameData>,
    pub(crate) observer_generation: u64,
    pub(crate) consecutive_failures: u32,
}

pub struct Attachment {
    pub id: u64,
    pub host_terminal_id: String,
    pub encoding: RenderEncoding,
    pub rows: u16,
    pub cols: u16,
    pub scroll: u32,
    pub reservation_id: Option<String>,
    pub mailbox: FrameMailbox,
    pub last_send: Instant,
    pub desynced: bool,
    pub delta_len: usize,
    pub delta_bytes: usize,
    /// Per-attachment diff state for `terminal_ansi` frames.
    pub(crate) encoder: BlitEncoder,
}

pub(crate) struct Inner {
    pub(crate) terminals: HashMap<Identity, TerminalSlot>,
    pub(crate) by_host_id: HashMap<String, Identity>,
    pub(crate) next_host_id: u64,
    pub(crate) reservations: HashMap<String, Reservation>,
    pub(crate) attachments: HashMap<u64, Attachment>,
    pub(crate) next_attachment: u64,
    control_owners: HashSet<u64>,
}

pub struct HostState {
    pub config: HostConfig,
    pub token: String,
    pub local_token: String,
    pub host_epoch: String,
    pub version: String,
    pub host_pid: u32,
    pub draining: AtomicBool,
    pub socket_dir_removed: AtomicBool,
    pub shutdown: watch::Sender<bool>,
    pub next_conn: AtomicU64,
    pub(crate) events: HostEvents,
    pub(crate) inner: Mutex<Inner>,
    pub(crate) polls: Mutex<HashMap<String, tokio::task::JoinHandle<()>>>,
}

impl HostState {
    pub fn new(
        config: HostConfig,
        token: String,
        local_token: String,
        host_epoch: String,
        version: String,
        host_pid: u32,
        shutdown: watch::Sender<bool>,
    ) -> Arc<Self> {
        let events = HostEvents::new(host_epoch.clone(), config.event_queue_bytes as usize);
        Arc::new(Self {
            config,
            token,
            local_token,
            host_epoch,
            version,
            host_pid,
            draining: AtomicBool::new(false),
            socket_dir_removed: AtomicBool::new(false),
            shutdown,
            next_conn: AtomicU64::new(1),
            events,
            polls: Mutex::new(HashMap::new()),
            inner: Mutex::new(Inner {
                terminals: HashMap::new(),
                by_host_id: HashMap::new(),
                next_host_id: 1,
                reservations: HashMap::new(),
                attachments: HashMap::new(),
                next_attachment: 1,
                control_owners: HashSet::new(),
            }),
        })
    }

    pub fn alloc_conn(&self) -> u64 {
        self.next_conn.fetch_add(1, Ordering::SeqCst)
    }

    pub async fn claim_control_owner(&self, conn_id: u64) {
        let mut inner = self.inner.lock().await;
        inner.control_owners.insert(conn_id);
    }

    pub async fn ping_json(&self) -> Value {
        json!({
            "ok": true,
            "host_epoch": self.host_epoch,
            "version": self.version,
            "host_pid": self.host_pid,
        })
    }

    pub async fn list_json(&self) -> Value {
        let inner = self.inner.lock().await;
        let (epoch, seq) = self.events.cursor().await;
        json!({ "ok": true, "terminals": list_rows(&inner), "epoch": epoch, "seq": seq })
    }

    pub async fn on_control_disconnect(&self, conn_id: u64) {
        let mut inner = self.inner.lock().await;
        inner.control_owners.remove(&conn_id);
        let drop_ids: Vec<String> = inner
            .reservations
            .iter()
            .filter(|(_, res)| res.conn_id == conn_id && !res.prepared)
            .map(|(id, _)| id.clone())
            .collect();
        for id in drop_ids {
            inner.reservations.remove(&id);
        }
    }

    pub async fn spawn(&self, conn_id: u64, extra: &Map<String, Value>) -> Value {
        if self.draining.load(Ordering::SeqCst) {
            return err("host_draining");
        }
        let terminal_id = s(extra, "terminal_id");
        let spawn_key = s(extra, "spawn_key");
        let reservation_id = s(extra, "reservation_id");
        let reserve_key = s(extra, "reserve_key");
        if terminal_id.is_empty()
            || spawn_key.is_empty()
            || reservation_id.is_empty()
            || reserve_key.is_empty()
        {
            return err("invalid_reservation");
        }
        let rows = extra.get("rows").and_then(Value::as_i64).unwrap_or(0);
        let cols = extra.get("cols").and_then(Value::as_i64).unwrap_or(0);
        let dims = match validate_dimensions(rows, cols) {
            Ok(dims) => dims,
            Err(e) => return err(e.code()),
        };
        let argv = extra
            .get("argv")
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let cwd = extra
            .get("cwd")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .unwrap_or_else(|| std::env::temp_dir());
        let env = extra
            .get("env")
            .and_then(Value::as_object)
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, v)| v.as_str().map(|val| (k.clone(), val.to_string())))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let deadline_ms = extra
            .get("commit_deadline_ms")
            .and_then(Value::as_u64)
            .unwrap_or(30_000);
        let identity = Identity {
            terminal_id: terminal_id.clone(),
            spawn_key: spawn_key.clone(),
        };
        let fingerprint = spawn_fingerprint(&argv, &env, &cwd, dims, &reservation_id, &reserve_key);
        {
            let inner = self.inner.lock().await;
            if let Some(existing) = inner.terminals.get(&identity) {
                if existing.fingerprint == fingerprint {
                    return json!({
                        "ok": true,
                        "method": "spawn_prepared",
                        "terminal_id": terminal_id,
                        "spawn_key": spawn_key,
                        "host_terminal_id": existing.host_terminal_id,
                        "pgid": existing.pgid,
                        "start_time": existing.start_time,
                        "reservation_id": existing.reservation_id,
                        "reserve_key": existing.reserve_key,
                        "reserve_generation": existing.reserve_generation,
                        "commit_state": match existing.commit_state {
                            CommitState::Prepared => "prepared",
                            CommitState::Committed => "committed",
                        },
                    });
                }
                return err("spawn_conflict");
            }
            let res = inner.reservations.get(&reservation_id);
            let Some(res) = res else {
                return err("invalid_reservation");
            };
            if res.key != reserve_key || res.terminal_id != terminal_id {
                return err("invalid_reservation");
            }
        }
        #[cfg(not(feature = "vt-engine"))]
        {
            let _ = (conn_id, deadline_ms, argv, cwd, env);
            return err("not_implemented");
        }
        #[cfg(feature = "vt-engine")]
        {
            let child = match spawn_prepared(
                dims.0,
                dims.1,
                &cwd,
                &argv,
                &env,
                self.config.native_scrollback_max_bytes as usize,
            ) {
                Ok(child) => child,
                Err(_) => return err("spawn_failed"),
            };
            let mut inner = self.inner.lock().await;
            if inner.terminals.contains_key(&identity) {
                drop(child);
                let existing = inner.terminals.get(&identity).expect("just inserted");
                return json!({
                    "ok": true,
                    "method": "spawn_prepared",
                    "terminal_id": terminal_id,
                    "spawn_key": spawn_key,
                    "host_terminal_id": existing.host_terminal_id,
                    "pgid": existing.pgid,
                    "start_time": existing.start_time,
                    "reservation_id": existing.reservation_id,
                    "reserve_key": existing.reserve_key,
                    "reserve_generation": existing.reserve_generation,
                });
            }
            let Some(res) = inner.reservations.get_mut(&reservation_id) else {
                drop(child);
                return err("invalid_reservation");
            };
            if res.key != reserve_key || res.terminal_id != terminal_id {
                drop(child);
                return err("invalid_reservation");
            }
            res.prepared = true;
            res.identity = Some(identity.clone());
            let generation = res.generation;
            let host_terminal_id = format!("ht-{}", inner.next_host_id);
            inner.next_host_id += 1;
            let slot = TerminalSlot {
                identity: identity.clone(),
                host_terminal_id: host_terminal_id.clone(),
                commit_state: CommitState::Prepared,
                pgid: child.pgid,
                start_time: child.start_time,
                title: String::new(),
                rows: dims.0,
                cols: dims.1,
                last_seq: 0,
                observation_state: ObservationState::Live,
                observation_reason: None,
                observation_generation: 1,
                fingerprint,
                reservation_id: reservation_id.clone(),
                reserve_key: reserve_key.clone(),
                reserve_generation: generation,
                observer_bind: ObserverBind::Reserved {
                    reservation_id: reservation_id.clone(),
                    generation,
                },
                commit_deadline: Some(Instant::now() + Duration::from_millis(deadline_ms)),
                child: Some(child),
                written_bytes: 0,
                dropped_bytes: 0,
                total_bytes: 0,
                truncated: false,
                user_attachments: HashSet::new(),
                locator: None,
                tmux_history_bytes: 0,
                history: None,
                last_frame: None,
                observer_generation: 0,
                consecutive_failures: 0,
            };
            inner
                .by_host_id
                .insert(host_terminal_id.clone(), identity.clone());
            inner.terminals.insert(identity, slot);
            let _ = conn_id;
            json!({
                "ok": true,
                "method": "spawn_prepared",
                "terminal_id": terminal_id,
                "spawn_key": spawn_key,
                "host_terminal_id": host_terminal_id,
                "pgid": inner.by_host_id.get(&host_terminal_id).and_then(|id| inner.terminals.get(id)).map(|t| t.pgid),
                "start_time": inner.by_host_id.get(&host_terminal_id).and_then(|id| inner.terminals.get(id)).map(|t| t.start_time),
                "reservation_id": reservation_id,
                "reserve_key": reserve_key,
                "reserve_generation": generation,
                "commit_state": "prepared",
            })
        }
    }

    pub async fn subscribe_events(&self, since: Option<u64>) -> (Value, EventReceiver) {
        self.events.subscribe(since).await
    }

    pub async fn attach(
        &self,
        host_terminal_id: &str,
        reservation_id: Option<String>,
        encoding: RenderEncoding,
        rows: u16,
        cols: u16,
    ) -> Result<(u64, FrameMailbox), &'static str> {
        let mut inner = self.inner.lock().await;
        let identity = inner
            .by_host_id
            .get(host_terminal_id)
            .cloned()
            .ok_or("not_found")?;
        let user_count = inner
            .terminals
            .get(&identity)
            .map(|s| s.user_attachments.len())
            .unwrap_or(0);
        if reservation_id.is_none() {
            if user_count as u32 >= self.config.max_attachments_per_terminal {
                return Err("capacity");
            }
            let queue_used = inner.attachments.len() as u32;
            if queue_used
                >= self
                    .config
                    .max_attachments_total
                    .saturating_sub(LIFECYCLE_RESERVED_SLOTS as u32)
            {
                return Err("capacity");
            }
        } else {
            let rid = reservation_id.as_deref().unwrap();
            let res = inner.reservations.get(rid).ok_or("invalid_reservation")?;
            if res.identity.as_ref() != Some(&identity) && res.terminal_id != identity.terminal_id {
                return Err("invalid_reservation");
            }
        }
        let mailbox = FrameMailbox::new();
        let id = inner.next_attachment;
        inner.next_attachment += 1;
        inner.attachments.insert(
            id,
            Attachment {
                id,
                host_terminal_id: host_terminal_id.to_string(),
                encoding,
                rows,
                cols,
                scroll: 0,
                reservation_id: reservation_id.clone(),
                mailbox: mailbox.clone(),
                last_send: Instant::now(),
                desynced: true,
                delta_len: 0,
                delta_bytes: 0,
                encoder: BlitEncoder::new(),
            },
        );
        if let Some(slot) = inner.terminals.get_mut(&identity) {
            if let Some(rid) = reservation_id {
                slot.observer_bind = ObserverBind::Bound {
                    reservation_id: rid,
                    generation: slot.reserve_generation,
                    attachment_id: id,
                };
            } else {
                slot.user_attachments.insert(id);
            }
        }
        Ok((id, mailbox))
    }

    pub async fn detach(&self, attachment_id: u64) {
        let mut inner = self.inner.lock().await;
        let Some(att) = inner.attachments.remove(&attachment_id) else {
            return;
        };
        att.mailbox.close();
        if let Some(identity) = inner.by_host_id.get(&att.host_terminal_id).cloned() {
            if let Some(slot) = inner.terminals.get_mut(&identity) {
                slot.user_attachments.remove(&attachment_id);
                if let ObserverBind::Bound {
                    reservation_id,
                    generation,
                    attachment_id: bound_id,
                } = &slot.observer_bind
                {
                    if *bound_id == attachment_id {
                        slot.observer_bind = ObserverBind::Entitled {
                            reservation_id: reservation_id.clone(),
                            generation: *generation,
                        };
                    }
                }
            }
        }
    }

    pub async fn set_viewport(
        &self,
        attachment_id: u64,
        rows: u16,
        cols: u16,
    ) -> Result<(), &'static str> {
        validate_dimensions(i64::from(rows), i64::from(cols)).map_err(|e| e.code())?;
        let mut inner = self.inner.lock().await;
        let Some(att) = inner.attachments.get_mut(&attachment_id) else {
            return Err("not_found");
        };
        att.rows = rows;
        att.cols = cols;
        att.desynced = true;
        Ok(())
    }

    pub async fn set_scroll(
        &self,
        attachment_id: u64,
        rows_from_live_edge: u32,
    ) -> Result<ServerMessage, &'static str> {
        let mut inner = self.inner.lock().await;
        let host_id = inner
            .attachments
            .get(&attachment_id)
            .ok_or("not_found")?
            .host_terminal_id
            .clone();
        let identity = inner.by_host_id.get(&host_id).cloned().ok_or("not_found")?;
        let max_rows = inner
            .terminals
            .get(&identity)
            .map(|slot| slot.config_scroll_max(self.config.native_scrollback_max_lines))
            .unwrap_or(0);
        let applied = rows_from_live_edge.min(max_rows);
        let Some(att) = inner.attachments.get_mut(&attachment_id) else {
            return Err("not_found");
        };
        att.scroll = applied;
        att.desynced = true;
        Ok(ServerMessage::ScrollOffsetApplied {
            applied_rows: applied,
            max_rows,
        })
    }
}

impl TerminalSlot {
    fn config_scroll_max(&self, max_lines: u32) -> u32 {
        #[cfg(feature = "vt-engine")]
        if let Some(child) = self.child.as_ref() {
            if let Some(metrics) = child.runtime.scroll_metrics() {
                return (metrics.max_offset_from_bottom as u32).min(max_lines);
            }
        }
        max_lines
    }
}
