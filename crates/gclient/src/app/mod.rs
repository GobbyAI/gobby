//! Workspace reducer: roster, focus-follows-control, attach, spawn.

mod apply;
mod attach;
mod attention;
mod live;
mod live_attach;
mod live_loop;
mod live_workspace;
mod pane;
pub mod project_tabs;
pub mod run_loop;
pub mod sidebar_model;
pub mod viewer_state;
mod window_state;
pub mod workspace_ops;

pub use attach::AttachState;
pub use live::{SidebarFetch, SidebarFetchFuture};
pub use live_loop::menu::{
    item_rects, menu_rect, ContextMenuKind, ContextMenuState, MenuAction, MenuItem,
};
pub use live_loop::modal_input::{apply_rename, route_modal_key, ModalOutcome};
pub use live_loop::mouse::{
    anchor_selection, extend_selection, finish_selection, route_mouse, ClickRun, MouseGesture,
    MouseOutcome, Placement, DOUBLE_CLICK_MS, MOUSE_SCROLL_LINES, PROJECT_DRAG_THRESHOLD,
    TAB_DRAG_THRESHOLD,
};
pub use live_loop::projects::{
    close_project, close_project_confirmed, create_worktree, focus_project,
    open_new_project_dialog, open_new_worktree_dialog, open_open_worktree_dialog,
    open_remove_worktree_dialog, open_worktree, remove_worktree, rename_project,
    submit_new_project,
};
pub use live_loop::run_live_loop;
pub use live_loop::sync_live_chrome;
pub use pane::{short_terminal_id, ControlState, Pane, PaneId};
pub use viewer_state::{PaneInterner, ViewerState};
pub use workspace_ops::WorkspaceModel;

use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::{
    Daemon, DaemonError, DaemonEvent, EventReceiver, Generation, LiveDaemon, RosterEntry,
    ScriptedDaemon, SidebarRows, Snapshot, TerminalRow,
};
use crate::frame_source::{
    AttachLocator, FrameDelivery, FrameError, FrameSource, PaneFrameSource, ScriptedFrameSource,
    Transport,
};
use crate::startup::AttachTarget;
use gobby_terminal::protocol::{ClientMessage, ServerMessage};
use serde_json::{json, Value};
use sidebar_model::{PendingSidebar, SidebarModel, SidebarStamps};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use tokio::sync::broadcast::error::TryRecvError;
use tokio::time::Instant;

const WORKSPACE_EVENT_BUFFER: usize = 1024;

#[derive(Debug, Clone)]
struct AttentionState {
    epoch: String,
    seq: u64,
    entries: Vec<RosterEntry>,
    applied_seqs: Vec<u64>,
}

impl AttentionState {
    fn empty() -> Self {
        Self {
            epoch: String::new(),
            seq: 0,
            entries: Vec::new(),
            applied_seqs: Vec::new(),
        }
    }
}

pub struct Workspace<D: Daemon = ScriptedDaemon> {
    project_id: Option<String>,
    daemon: D,
    panes: HashMap<PaneId, Pane>,
    order: Vec<PaneId>,
    focus: Option<PaneId>,
    next_pane: u32,
    roster_ids: Vec<String>,
    /// Pane order the window set (`set_tab_order`); the next roster page
    /// follows it. Per-window memory, never persisted.
    pane_order: Vec<String>,
    /// The workspace this window attaches to (`--node`/`--workspace`).
    attach: AttachTarget,
    /// The window runs inside a gclient pane and opens no terminal of its
    /// own.
    in_pane: bool,
    attention: AttentionState,
    /// This machine's id, the default home for an agent without one.
    local_machine: String,
    /// The last rows fetched for the sidebar; `sidebar` is built from them.
    sidebar_rows: SidebarRows,
    sidebar: SidebarModel,
    git_refreshed_at: Instant,
    pending_sidebar: PendingSidebar,
    sidebar_stamps: SidebarStamps,
    pending_attention: Option<attention::PendingAttention>,
    gobby_home: Option<PathBuf>,
    /// Where gclient was started; shells of a project with no checkout
    /// here begin in it.
    launch_dir: Option<PathBuf>,
    frame_delivery: FrameDelivery,
    lifecycle: Option<Snapshot>,
    daemon_ready: bool,
    daemon_error: Option<DaemonError>,
    event_rx: Option<EventReceiver>,
    attached_generation: HashMap<PaneId, Generation>,
    pending_spawns: HashSet<String>,
    status_message: Option<String>,
    exit_reason: Option<String>,
    shutdown_started: bool,
    /// The attached daemon workspace, once `workspace_attach` replied.
    workspace_model: Option<WorkspaceModel>,
    /// Terminals whose `tab.create` or `pane.split` op is in flight: the
    /// daemon's event places them, and this window follows it there.
    pending_placements: HashSet<String>,
    /// `(tab, pane)` of the panes pending placements landed in, drained by
    /// the chrome sync that focuses them.
    placed_panes: Vec<(String, String)>,
}

impl<D: Daemon> crate::teardown::ShutdownWorkspace for Workspace<D> {
    fn begin_shutdown(&mut self) -> bool {
        if self.shutdown_started {
            false
        } else {
            self.shutdown_started = true;
            true
        }
    }

    fn shutdown_reason(&self) -> &str {
        self.exit_reason.as_deref().unwrap_or("client exiting")
    }

    fn take_shutdown_requests(&mut self) -> Vec<Value> {
        let mut releases = Vec::new();
        let mut detaches = Vec::new();
        for pane_id in &self.order {
            let Some(pane) = self.panes.get(pane_id) else {
                continue;
            };
            let attachment_id = pane.attachment_id();
            if pane.is_held() {
                releases.push(json!({
                    "type": "terminal_release_control",
                    "request_id": uuid::Uuid::new_v4().to_string(),
                    "terminal_id": pane.terminal_id,
                    "attachment_id": attachment_id,
                }));
            }
            if matches!(pane.attach_state(), AttachState::Attached { .. }) {
                detaches.push(json!({
                    "type": "terminal_detach",
                    "request_id": uuid::Uuid::new_v4().to_string(),
                    "terminal_id": pane.terminal_id,
                    "attachment_id": attachment_id,
                }));
            }
        }
        releases.extend(detaches);
        releases
    }
}

impl Workspace {
    pub fn scripted() -> Self {
        Self {
            project_id: None,
            daemon: ScriptedDaemon::new(),
            panes: HashMap::new(),
            order: Vec::new(),
            focus: None,
            next_pane: 1,
            roster_ids: Vec::new(),
            pane_order: Vec::new(),
            attach: AttachTarget::default(),
            in_pane: false,
            attention: AttentionState::empty(),
            local_machine: String::new(),
            sidebar_rows: SidebarRows::default(),
            sidebar: SidebarModel::default(),
            git_refreshed_at: Instant::now(),
            pending_sidebar: PendingSidebar::default(),
            sidebar_stamps: SidebarStamps::default(),
            pending_attention: None,
            gobby_home: None,
            launch_dir: None,
            frame_delivery: FrameDelivery::Auto,
            lifecycle: None,
            daemon_ready: true,
            daemon_error: None,
            event_rx: None,
            attached_generation: HashMap::new(),
            pending_spawns: HashSet::new(),
            status_message: None,
            exit_reason: None,
            shutdown_started: false,
            workspace_model: None,
            pending_placements: HashSet::new(),
            placed_panes: Vec::new(),
        }
    }

    pub fn daemon(&self) -> &ScriptedDaemon {
        &self.daemon
    }

    pub fn daemon_mut(&mut self) -> &mut ScriptedDaemon {
        &mut self.daemon
    }

    pub fn project_id(&self) -> Option<&str> {
        self.project_id.as_deref()
    }

    pub fn select_project(&mut self, project_id: impl Into<String>) {
        self.project_id = Some(project_id.into());
    }

    pub fn set_daemon_reachable(&mut self, reachable: bool) {
        self.daemon.set_reachable(reachable);
        if !reachable {
            let generation = Daemon::subscribe(&self.daemon).0.generation;
            self.observe_daemon_disconnect(
                generation,
                DaemonError::Unavailable { retry_after: None },
            );
        }
    }

    pub fn reconcile_subscribe_first(&mut self) -> Result<(), DaemonError> {
        self.daemon.subscribe();
        let buffered = self.daemon.take_attention_events();
        let roster = self.daemon.fetch_attention_roster()?;
        self.install_roster(&roster);
        for event in buffered {
            self.ingest_attention(event)?;
        }
        self.sidebar_rows = self.daemon.sidebar_rows();
        self.rebuild_sidebar();
        Ok(())
    }

    fn install_roster(&mut self, roster: &Value) {
        self.attention.epoch = roster
            .get("epoch")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.attention.seq = roster.get("seq").and_then(Value::as_u64).unwrap_or(0);
        self.attention.entries = roster
            .get("entries")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|entry| serde_json::from_value(entry.clone()).ok())
            .collect();
        self.attention.applied_seqs = vec![self.attention.seq];
        self.rebuild_sidebar();
    }

    pub(super) fn ingest_attention(&mut self, event: Value) -> Result<(), DaemonError> {
        let epoch = event
            .get("epoch")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let seq = event.get("seq").and_then(Value::as_u64).unwrap_or(0);
        if !self.attention.epoch.is_empty() && epoch != self.attention.epoch {
            let roster = self.daemon.fetch_attention_roster()?;
            self.install_roster(&roster);
            return Ok(());
        }
        if epoch == self.attention.epoch && seq <= self.attention.seq {
            return Ok(());
        }
        self.note_attention_event(&event);
        self.attention.seq = self.attention.seq.max(seq);
        self.attention.applied_seqs.push(seq);
        self.rebuild_sidebar();
        Ok(())
    }

    pub fn fetch_roster(&mut self) -> Result<(), DaemonError> {
        let project = self.project_id.clone().unwrap_or_default();
        let mut cursor: Option<String> = None;
        let mut rows = Vec::new();
        let mut roster_ids = Vec::new();
        loop {
            let page = self
                .daemon
                .fetch_terminals_page(&project, cursor.as_deref())?;
            if let Some(items) = page.get("items").and_then(Value::as_array) {
                for item in items {
                    let id = item
                        .get("id")
                        .or_else(|| item.get("terminal_id"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if !id.is_empty() && !roster_ids.iter().any(|known| known == id) {
                        roster_ids.push(id.to_string());
                        rows.push(item.clone());
                    }
                }
            }
            match page.get("next_cursor").and_then(Value::as_str) {
                Some(next) if !next.is_empty() => cursor = Some(next.to_string()),
                _ => break,
            }
        }
        self.reconcile_terminal_rows(&rows, roster_ids)?;
        Ok(())
    }

    fn reconcile_terminal_rows(
        &mut self,
        rows: &[Value],
        roster_ids: Vec<String>,
    ) -> Result<(), DaemonError> {
        let removed: Vec<String> = self
            .order
            .iter()
            .map(|id| self.panes[id].terminal_id.clone())
            .filter(|id| !roster_ids.contains(id))
            .collect();
        for terminal_id in removed {
            self.remove_terminal(&terminal_id);
        }
        for row in rows {
            let terminal_id = row
                .get("terminal_id")
                .or_else(|| row.get("id"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            if terminal_id.is_empty() {
                continue;
            }
            self.pending_spawns.remove(terminal_id);
            if self.pane_for_terminal(terminal_id).is_none() {
                let backend = row
                    .get("backend")
                    .and_then(Value::as_str)
                    .unwrap_or("native");
                self.open_terminal(terminal_id, backend, "")
                    .map_err(|error| DaemonError::Protocol {
                        detail: error.to_string(),
                    })?;
            }
        }
        self.pending_spawns
            .retain(|terminal_id| roster_ids.contains(terminal_id));
        self.roster_ids = roster_ids;
        Ok(())
    }

    pub fn open_terminal(
        &mut self,
        terminal_id: &str,
        backend: &str,
        epoch: &str,
    ) -> Result<PaneId, FrameError> {
        let id = PaneId(self.next_pane);
        self.next_pane += 1;
        let mut pane = Pane::new(id, terminal_id, backend, epoch);
        // A scripted run has no daemon row to carry a title, and its terminal
        // ids are names rather than UUIDs, so the id is the honest title here.
        pane.title = terminal_id.to_string();
        self.panes.insert(id, pane);
        self.order.push(id);
        if !self.roster_ids.iter().any(|t| t == terminal_id) {
            self.roster_ids.push(terminal_id.to_string());
        }
        // The agent rows name a terminal after its pane, so a pane opened
        // after the roster landed re-derives its row.
        self.rebuild_sidebar();
        self.attach_frames(id)?;
        Ok(id)
    }

    pub fn attach_frames(&mut self, id: PaneId) -> Result<(), FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let locator = self.locator_for(id);
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.scripted_source_mut()
            .ok_or_else(|| FrameError::Protocol("pane does not have a scripted source".into()))?
            .connect(&locator, 80, 24)?;
        let attachment = pane.attachment_id().to_string();
        let terminal_id = pane.terminal_id.clone();
        let _ = self.daemon.send_ws(json!({
            "type": "terminal_attach",
            "request_id": format!("req-{attachment}"),
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "frame_delivery": "direct",
            "viewer": "gclient"
        }));
        self.push_frame(id, "first-frame");
        Ok(())
    }

    fn locator_for(&self, id: PaneId) -> AttachLocator {
        let pane = &self.panes[&id];
        AttachLocator {
            backend: pane.backend.clone(),
            frame_host_epoch: pane.expected_host_epoch.clone(),
            host_terminal_id: pane.terminal_id.clone(),
            frame_socket_path: "/tmp/gterm-frames.sock".into(),
            pane: None,
        }
    }

    pub fn push_frame(&mut self, id: PaneId, _bytes: &str) {
        let pane = self.panes.get_mut(&id).expect("pane");
        if pane.scroll_offset > 0 {
            pane.new_output = true;
        }
        pane.frames_rendered += 1;
    }

    pub fn focus_pane(&mut self, id: PaneId) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        if let Some(prev) = self.focus {
            if prev != id {
                self.release_control(prev)?;
            }
        }
        self.focus = Some(id);
        if self.daemon.reachable() {
            self.take_control(id)?;
        } else {
            let pane = self.panes.get_mut(&id).expect("pane");
            pane.control = ControlState::Observe;
        }
        Ok(())
    }

    pub fn take_control(&mut self, id: PaneId) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        let pane = self.panes.get(&id).expect("pane");
        if !pane.is_live() {
            return Err(DaemonError::new(
                409,
                "stale_attachment",
                "attachment is not live",
            ));
        }
        let attachment = pane.attachment_id().to_string();
        let terminal_id = pane.terminal_id.clone();
        if !self.daemon.reachable() {
            return Err(DaemonError::new(503, "unreachable", "daemon unreachable"));
        }
        self.daemon.send_ws(json!({
            "type": "terminal_take_control",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "takeover": false
        }))?;
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.control = ControlState::Held;
        pane.take_back = false;
        pane.set_lease_generation(pane.lease_generation().max(1));
        Ok(())
    }

    pub fn release_control(&mut self, id: PaneId) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        let pane = self.panes.get(&id).expect("pane");
        let attachment = pane.attachment_id().to_string();
        let terminal_id = pane.terminal_id.clone();
        if self.daemon.ws_connected() && self.daemon.reachable() {
            let _ = self.daemon.send_ws(json!({
                "type": "terminal_release_control",
                "terminal_id": terminal_id,
                "attachment_id": attachment
            }));
        }
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.control = ControlState::Observe;
        pane.take_back = false;
        Ok(())
    }

    pub fn send_keys(&mut self, id: PaneId, data: &str) -> Result<(), DaemonError> {
        self.send_input(id, data.as_bytes())
    }

    pub fn send_input(&mut self, id: PaneId, data: &[u8]) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        if !self.panes[&id].writable() {
            let pane = self.panes.get_mut(&id).expect("pane");
            if !pane.is_live() {
                return Err(DaemonError::new(
                    409,
                    "stale_attachment",
                    "attachment is not live",
                ));
            }
            if matches!(
                pane.control,
                ControlState::LeaseLost | ControlState::UncertainReadOnly
            ) {
                return Err(DaemonError::new(
                    403,
                    "read_only",
                    "pane requires explicit control recovery",
                ));
            }
            if pane.pending_input.is_some() {
                return Err(DaemonError::new(
                    409,
                    "control_pending",
                    "a take-control request is already pending",
                ));
            }
            pane.pending_input = Some(data.to_vec());
            let attachment = pane.attachment_id().to_string();
            let terminal_id = pane.terminal_id.clone();
            return self.daemon.send_ws(json!({
                "type": "terminal_take_control",
                "terminal_id": terminal_id,
                "attachment_id": attachment,
                "takeover": false
            }));
        }
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.client_write_seq += 1;
        let seq = pane.client_write_seq;
        pane.in_flight_write = Some(seq);
        let attachment = pane.attachment_id().to_string();
        let terminal_id = pane.terminal_id.clone();
        let data = String::from_utf8_lossy(data);
        self.daemon.send_ws(json!({
            "type": "terminal_input",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "data": data,
            "client_write_seq": seq
        }))
    }

    pub fn paste_to_pty(&mut self, id: PaneId, text: &str) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        if text.len() > PASTE_MAX_BYTES {
            return Err(DaemonError::new(400, "paste_too_large", "paste_too_large"));
        }
        let pane = self.panes.get_mut(&id).expect("pane");
        if pane.copy_search {
            pane.search_buffer.push_str(text);
            return Ok(());
        }
        if !pane.writable() {
            return Err(DaemonError::new(403, "held", "paste refused"));
        }
        pane.client_write_seq += 1;
        let seq = pane.client_write_seq;
        pane.in_flight_write = Some(seq);
        let attachment = pane.attachment_id().to_string();
        let terminal_id = pane.terminal_id.clone();
        self.daemon.send_ws(json!({
            "type": "terminal_paste",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "text": text,
            "client_write_seq": seq
        }))
    }

    pub fn paste_local(&mut self, id: PaneId, text: &str) -> Result<(), DaemonError> {
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.search_buffer.push_str(text);
        Ok(())
    }

    pub fn enter_copy_search(&mut self, id: PaneId) {
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.copy_search = true;
    }

    pub fn set_bracketed_paste(&mut self, id: PaneId, on: bool) {
        self.panes.get_mut(&id).expect("pane").bracketed_paste = on;
    }

    pub fn force_held(&mut self, id: PaneId) {
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.control = ControlState::Held;
        pane.take_back = false;
    }

    pub fn seed_attach_history(&mut self, id: PaneId, text: &str) {
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.attach_history = Some(text.to_string());
        pane.copy_seeded_from_history = true;
        pane.required_created_flag = false;
    }

    pub fn set_scroll_offset(&mut self, id: PaneId, rows: u32) -> Result<(), FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let backend = self.panes[&id].backend.clone();
        if backend == "native" {
            self.panes
                .get_mut(&id)
                .expect("pane")
                .scripted_source_mut()
                .ok_or_else(|| FrameError::Protocol("pane does not have a scripted source".into()))?
                .send(&ClientMessage::SetScrollOffset {
                    rows_from_live_edge: rows,
                })?;
        }
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.scroll_offset = rows;
        if rows == 0 {
            pane.new_output = false;
        }
        Ok(())
    }

    pub fn apply_scroll_applied(&mut self, id: PaneId, applied: u32, max_rows: u32) {
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.scroll_offset = applied;
        pane.max_scroll = max_rows;
    }

    pub fn jump_to_bottom(&mut self, id: PaneId) -> Result<(), FrameError> {
        self.set_scroll_offset(id, 0)
    }

    pub fn drop_and_reconnect_frames(&mut self, id: PaneId) -> Result<(), FrameError> {
        let offset = self.panes[&id].scroll_offset;
        self.attach_frames(id)?;
        self.panes.get_mut(&id).expect("pane").scroll_offset = offset;
        Ok(())
    }

    pub fn kill_frame_stream(&mut self, id: PaneId) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        let (attachment, terminal_id) = {
            let pane = self.panes.get_mut(&id).expect("pane");
            let Some((attachment, _)) = pane.begin_detaching(tokio::time::Instant::now()) else {
                return Ok(());
            };
            (attachment, pane.terminal_id.clone())
        };
        self.daemon.send_ws(json!({
            "type": "terminal_detach",
            "request_id": format!("req-detach-{attachment}"),
            "terminal_id": terminal_id,
            "attachment_id": attachment
        }))?;
        Ok(())
    }

    pub fn reattach_frames(&mut self, id: PaneId) -> Result<(), FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let locator = self.locator_for(id);
        let mut source = ScriptedFrameSource::new(Transport::Proxy);
        source.set_welcome_epoch(locator.frame_host_epoch.clone());
        source.connect(&locator, 80, 24)?;
        let attachment = uuid::Uuid::new_v4().to_string();
        let terminal_id = self.panes[&id].terminal_id.clone();
        let generation = Daemon::subscribe(&self.daemon).0.generation;
        {
            let pane = self.panes.get_mut(&id).expect("pane");
            pane.install_attachment(
                attachment.clone(),
                Transport::Proxy,
                generation,
                0,
                PaneFrameSource::Scripted(source),
            );
        }
        self.daemon.send_ws(json!({
            "type": "terminal_attach",
            "request_id": format!("req-{attachment}"),
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "frame_delivery": "proxy",
            "encoding": "semantic_frame",
            "viewer": "gclient"
        }))?;
        self.push_frame(id, "first-frame");
        Ok(())
    }

    pub fn drop_daemon_ws(&mut self) {
        self.daemon.set_ws_connected(false);
        let generation = Daemon::subscribe(&self.daemon).0.generation;
        self.observe_daemon_disconnect(generation, DaemonError::Unavailable { retry_after: None });
    }

    pub fn reconnect_daemon_ws(&mut self) -> Result<(), DaemonError> {
        self.ensure_not_exiting()?;
        self.daemon.set_ws_connected(true);
        self.daemon_ready = true;
        self.daemon_error = None;
        let generation = Daemon::subscribe(&self.daemon).0.generation;
        let ids: Vec<PaneId> = self.order.clone();
        for id in ids {
            let attachment = uuid::Uuid::new_v4().to_string();
            let pane = self.panes.get_mut(&id).expect("pane");
            let mut source = ScriptedFrameSource::new(Transport::Direct);
            source.set_welcome_epoch(pane.expected_host_epoch.clone());
            pane.install_attachment(
                attachment.clone(),
                Transport::Direct,
                generation,
                0,
                PaneFrameSource::Scripted(source),
            );
            let terminal_id = pane.terminal_id.clone();
            self.daemon.send_ws(json!({
                "type": "terminal_attach",
                "request_id": format!("req-{attachment}"),
                "terminal_id": terminal_id,
                "attachment_id": attachment,
                "frame_delivery": "direct",
                "viewer": "gclient"
            }))?;
        }
        Ok(())
    }

    pub fn respond(&mut self, entry_id: &str, body: Value) -> Result<Value, DaemonError> {
        self.ensure_requests_allowed()?;
        self.daemon.respond(entry_id, body)
    }

    pub fn spawn_agent(&mut self, body: Value) -> Result<String, FrameError> {
        self.ensure_requests_allowed()
            .map_err(|error| FrameError::Other(error.to_string()))?;
        let result = self
            .daemon
            .spawn_agent(body)
            .map_err(|err| FrameError::Other(err.to_string()))?;
        if result.get("success").and_then(Value::as_bool) == Some(false) {
            let reason = result
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("spawn refused");
            self.status_message = Some(reason.to_string());
            return Err(FrameError::Other(reason.to_string()));
        }
        let terminal_id = result
            .get("terminal_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if terminal_id.is_empty() {
            return Err(FrameError::Other("spawn returned no terminal id".into()));
        }
        if self.pane_for_terminal(&terminal_id).is_none() {
            self.pending_spawns.insert(terminal_id.clone());
        }
        Ok(terminal_id)
    }

    pub fn terminate_terminal(&mut self, terminal_id: &str) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        self.daemon.send_ws(json!({
            "type": "terminal_kill",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": terminal_id
        }))
    }

    pub fn apply_ws(&mut self, message: &Value) -> Result<(), DaemonError> {
        if self.exit_reason.is_some() {
            return Ok(());
        }
        apply::apply(self, message)
    }
}

impl<D: Daemon> Workspace<D> {
    pub fn retire_indeterminate_control(
        &mut self,
        pane_id: PaneId,
        now: tokio::time::Instant,
    ) -> Option<(String, String, Generation)> {
        let pane = self.panes.get_mut(&pane_id)?;
        let terminal_id = pane.terminal_id.clone();
        let (attachment_id, generation) = pane.begin_detaching(now)?;
        pane.clear_control("Control result indeterminate.");
        Some((terminal_id, attachment_id, generation))
    }

    pub fn observe_daemon_disconnect(&mut self, _generation: Generation, error: DaemonError) {
        self.daemon_ready = false;
        for pane in self.panes.values_mut() {
            pane.clear_control(error.to_string());
        }
        self.daemon_error = Some(error);
    }

    pub fn submit_expired_detaches(
        &mut self,
        supervisor: &mut run_loop::ReconnectSupervisor,
        now: tokio::time::Instant,
    ) -> usize {
        if self.exit_reason.is_some() {
            return 0;
        }
        let mut submitted = 0;
        for pane in self.panes.values_mut() {
            if let Some(generation) = pane.take_expired_detach_generation(now) {
                drop(
                    supervisor.request(generation, &DaemonError::Unavailable { retry_after: None }),
                );
                submitted += 1;
            }
        }
        submitted
    }

    fn ensure_requests_allowed(&self) -> Result<(), DaemonError> {
        self.ensure_not_exiting()?;
        if !self.daemon_ready {
            return Err(self
                .daemon_error
                .clone()
                .unwrap_or(DaemonError::Unavailable { retry_after: None }));
        }
        Ok(())
    }

    fn ensure_not_exiting(&self) -> Result<(), DaemonError> {
        if self.exit_reason.is_some() {
            return Err(DaemonError::Protocol {
                detail: "Client exit is latched.".to_string(),
            });
        }
        Ok(())
    }

    pub fn latch_exit(&mut self, reason: impl Into<String>) -> bool {
        if self.exit_reason.is_some() {
            return false;
        }
        let reason = reason.into();
        tracing::info!(
            lifecycle_stage = "latch-exit",
            reason = %reason,
            "gclient exit latched"
        );
        self.exit_reason = Some(reason);
        true
    }

    pub fn exit_reason(&self) -> Option<&str> {
        self.exit_reason.as_deref()
    }

    pub fn status_message(&self) -> Option<&str> {
        self.status_message.as_deref()
    }

    pub fn pane(&self, id: PaneId) -> &Pane {
        &self.panes[&id]
    }

    pub fn pane_mut(&mut self, id: PaneId) -> &mut Pane {
        self.panes.get_mut(&id).expect("pane exists")
    }

    pub fn pane_for_terminal(&self, terminal_id: &str) -> Option<PaneId> {
        self.order
            .iter()
            .copied()
            .find(|id| self.panes[id].terminal_id == terminal_id)
    }

    pub fn pane_by_attachment(&self, attachment_id: &str) -> Option<&Pane> {
        self.panes
            .values()
            .find(|pane| !attachment_id.is_empty() && pane.attachment_id() == attachment_id)
    }

    pub fn replace_frame_source(
        &mut self,
        id: PaneId,
        source: PaneFrameSource,
    ) -> Result<(), FrameError> {
        let pane = self
            .panes
            .get_mut(&id)
            .ok_or_else(|| FrameError::Protocol("unknown pane".into()))?;
        pane.install_frame_source(source);
        Ok(())
    }

    pub async fn recv_pane_frame(&mut self, id: PaneId) -> Result<ServerMessage, FrameError> {
        let result = self
            .panes
            .get_mut(&id)
            .and_then(Pane::frame_source_mut)
            .ok_or_else(|| FrameError::Protocol("pane has no frame source".into()))?
            .recv()
            .await;
        if let Ok(message) = &result {
            self.record_source_message(id, message);
        }
        result
    }

    fn record_source_message(&mut self, id: PaneId, message: &ServerMessage) {
        let pane = self.panes.get_mut(&id).expect("pane exists");
        match message {
            ServerMessage::Frame(frame) => {
                pane.latest_frame = Some(frame.clone());
                pane.frames_rendered = pane.frames_rendered.saturating_add(1);
                if pane.scroll_offset > 0 {
                    pane.new_output = true;
                }
            }
            ServerMessage::Terminal(_) | ServerMessage::Graphics { .. } => {
                pane.frames_rendered = pane.frames_rendered.saturating_add(1);
                if pane.scroll_offset > 0 {
                    pane.new_output = true;
                }
            }
            ServerMessage::AttachHistory { text, .. } => {
                pane.attach_history = Some(text.clone());
                pane.copy_seeded_from_history = true;
            }
            ServerMessage::ScrollOffsetApplied {
                applied_rows,
                max_rows,
            } => {
                pane.scroll_offset = *applied_rows;
                pane.max_scroll = *max_rows;
                if *applied_rows == 0 {
                    pane.new_output = false;
                }
            }
            _ => {}
        }
    }

    pub fn pane_count(&self) -> usize {
        self.panes.len()
    }

    pub fn attention_applied_seqs(&self) -> Vec<u64> {
        self.attention.applied_seqs.clone()
    }

    pub fn attention_epoch(&self) -> &str {
        &self.attention.epoch
    }

    pub(super) fn pane_for_attachment_mut(&mut self, attachment_id: &str) -> Option<&mut Pane> {
        self.panes
            .values_mut()
            .find(|pane| pane.is_live() && pane.attachment_id() == attachment_id)
    }

    pub(super) fn retire_attachment(&mut self, attachment_id: &str, reason: Option<&str>) -> bool {
        self.panes
            .values_mut()
            .any(|pane| pane.retire_attachment(attachment_id, reason.map(ToOwned::to_owned)))
    }

    pub(super) fn remove_terminal(&mut self, terminal_id: &str) {
        self.pending_spawns.remove(terminal_id);
        let ids: Vec<PaneId> = self
            .order
            .iter()
            .copied()
            .filter(|id| self.panes[id].terminal_id == terminal_id)
            .collect();
        for id in ids {
            self.panes.remove(&id);
            self.order.retain(|existing| *existing != id);
            if self.focus == Some(id) {
                self.focus = None;
            }
        }
        self.roster_ids.retain(|id| id != terminal_id);
    }
}
