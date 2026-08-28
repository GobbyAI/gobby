//! Workspace reducer: roster, focus-follows-control, attach, spawn.

mod apply;
mod pane;

pub use pane::{ControlState, Pane, PaneId};

use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::{DaemonError, ScriptedDaemon};
use crate::frame_source::{AttachLocator, FrameError, FrameSource, ScriptedFrameSource};
use crate::persist::{load_snapshot, WorkspaceSnapshot};
use gobby_terminal::protocol::ClientMessage;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Debug, Clone)]
struct AttentionState {
    epoch: String,
    seq: u64,
    entries: Vec<String>,
    applied_seqs: Vec<u64>,
}

pub struct Workspace {
    project_id: Option<String>,
    daemon: ScriptedDaemon,
    frames: ScriptedFrameSource,
    panes: HashMap<PaneId, Pane>,
    order: Vec<PaneId>,
    focus: Option<PaneId>,
    next_pane: u32,
    roster_ids: Vec<String>,
    attention: AttentionState,
    gobby_home: Option<PathBuf>,
}

impl Workspace {
    pub fn scripted() -> Self {
        Self::with_frames(ScriptedFrameSource::new())
    }

    pub fn with_frames(frames: ScriptedFrameSource) -> Self {
        Self {
            project_id: None,
            daemon: ScriptedDaemon::new(),
            frames,
            panes: HashMap::new(),
            order: Vec::new(),
            focus: None,
            next_pane: 1,
            roster_ids: Vec::new(),
            attention: AttentionState {
                epoch: String::new(),
                seq: 0,
                entries: Vec::new(),
                applied_seqs: Vec::new(),
            },
            gobby_home: None,
        }
    }

    pub fn daemon(&self) -> &ScriptedDaemon {
        &self.daemon
    }

    pub fn daemon_mut(&mut self) -> &mut ScriptedDaemon {
        &mut self.daemon
    }

    pub fn frames(&self) -> &ScriptedFrameSource {
        &self.frames
    }

    pub fn project_id(&self) -> Option<&str> {
        self.project_id.as_deref()
    }

    pub fn select_project(&mut self, project_id: impl Into<String>) {
        self.project_id = Some(project_id.into());
    }

    pub fn set_gobby_home(&mut self, home: PathBuf) {
        self.gobby_home = Some(home);
    }

    pub fn set_daemon_reachable(&mut self, reachable: bool) {
        self.daemon.set_reachable(reachable);
        if !reachable {
            for pane in self.panes.values_mut() {
                if pane.control == ControlState::Held {
                    pane.control = ControlState::Observe;
                }
            }
        }
    }

    pub fn pane(&self, id: PaneId) -> &Pane {
        &self.panes[&id]
    }

    pub fn pane_count(&self) -> usize {
        self.panes.len()
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
            .find(|pane| pane.attachment_id == attachment_id)
    }

    pub fn roster_terminal_ids(&self) -> Vec<String> {
        self.roster_ids.clone()
    }

    pub fn attention_entry_ids(&self) -> Vec<String> {
        self.attention.entries.clone()
    }

    pub fn attention_applied_seqs(&self) -> Vec<u64> {
        self.attention.applied_seqs.clone()
    }

    pub fn attention_epoch(&self) -> &str {
        &self.attention.epoch
    }

    pub fn reconcile_subscribe_first(&mut self) -> Result<(), DaemonError> {
        self.daemon.subscribe();
        let buffered = self.daemon.take_attention_events();
        let roster = self.daemon.fetch_attention_roster()?;
        self.install_roster(&roster);
        for event in buffered {
            self.ingest_attention(event)?;
        }
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
            .filter_map(|entry| entry.get("entry_id")?.as_str().map(str::to_string))
            .collect();
        self.attention.applied_seqs = vec![self.attention.seq];
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
        if let Some(id) = event.get("entry_id").and_then(Value::as_str) {
            if !self.attention.entries.iter().any(|e| e == id) {
                self.attention.entries.push(id.to_string());
            }
        }
        self.attention.seq = self.attention.seq.max(seq);
        self.attention.applied_seqs.push(seq);
        Ok(())
    }

    pub fn fetch_roster(&mut self) -> Result<(), DaemonError> {
        let project = self.project_id.clone().unwrap_or_default();
        let mut cursor: Option<String> = None;
        self.roster_ids.clear();
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
                    if !id.is_empty() {
                        self.roster_ids.push(id.to_string());
                    }
                }
            }
            match page.get("next_cursor").and_then(Value::as_str) {
                Some(next) if !next.is_empty() => cursor = Some(next.to_string()),
                _ => break,
            }
        }
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
        let pane = Pane::new(id, terminal_id, backend, epoch);
        self.panes.insert(id, pane);
        self.order.push(id);
        if !self.roster_ids.iter().any(|t| t == terminal_id) {
            self.roster_ids.push(terminal_id.to_string());
        }
        self.attach_frames(id)?;
        Ok(id)
    }

    pub fn attach_frames(&mut self, id: PaneId) -> Result<(), FrameError> {
        let locator = self.locator_for(id);
        self.frames.connect(&locator, 80, 24)?;
        let attach = crate::views::observe_tmux_pane(&locator).1;
        self.frames.send(&attach)?;
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.live = true;
        let attachment = pane.attachment_id.clone();
        let terminal_id = pane.terminal_id.clone();
        let _ = self.daemon.send_ws(json!({
            "type": "terminal_attach",
            "request_id": format!("req-{attachment}"),
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "frame_delivery": "direct"
        }));
        self.push_frame(id, "first-frame");
        Ok(())
    }

    pub fn attach_locator(&mut self, locator: AttachLocator) -> Result<String, FrameError> {
        let epoch = self.frames.connect(&locator, 80, 24)?;
        let attach = crate::views::observe_tmux_pane(&locator).1;
        self.frames.send(&attach)?;
        Ok(epoch)
    }

    fn locator_for(&self, id: PaneId) -> AttachLocator {
        let pane = &self.panes[&id];
        AttachLocator {
            backend: pane.backend.clone(),
            frame_host_epoch: pane.expected_host_epoch.clone(),
            host_terminal_id: pane.terminal_id.clone(),
            socket_path: "/tmp/gterm-frames.sock".into(),
            pane_id: None,
            server_pid: None,
            server_start_time: None,
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
        let pane = self.panes.get(&id).expect("pane");
        if !pane.live {
            return Err(DaemonError::new(
                409,
                "stale_attachment",
                "attachment is not live",
            ));
        }
        let attachment = pane.attachment_id.clone();
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
        pane.lease_generation = pane.lease_generation.max(1);
        Ok(())
    }

    pub fn release_control(&mut self, id: PaneId) -> Result<(), DaemonError> {
        let pane = self.panes.get(&id).expect("pane");
        let attachment = pane.attachment_id.clone();
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
        let pane = self.panes.get_mut(&id).expect("pane");
        if !pane.writable() {
            return Err(DaemonError::new(403, "held", "pane is not writable"));
        }
        pane.client_write_seq += 1;
        let seq = pane.client_write_seq;
        pane.in_flight_write = Some(seq);
        let attachment = pane.attachment_id.clone();
        let terminal_id = pane.terminal_id.clone();
        self.daemon.send_ws(json!({
            "type": "terminal_input",
            "terminal_id": terminal_id,
            "attachment_id": attachment,
            "data": data,
            "client_write_seq": seq
        }))
    }

    pub fn paste_to_pty(&mut self, id: PaneId, text: &str) -> Result<(), DaemonError> {
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
        let attachment = pane.attachment_id.clone();
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
        pane.live = true;
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
        let backend = self.panes[&id].backend.clone();
        if backend == "native" {
            self.frames.send(&ClientMessage::SetScrollOffset {
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
        let pane = self.panes.get(&id).expect("pane");
        let attachment = pane.attachment_id.clone();
        let terminal_id = pane.terminal_id.clone();
        self.daemon.send_ws(json!({
            "type": "terminal_detach",
            "request_id": format!("req-detach-{attachment}"),
            "terminal_id": terminal_id,
            "attachment_id": attachment
        }))?;
        let pane = self.panes.get_mut(&id).expect("pane");
        pane.live = false;
        pane.control = ControlState::Observe;
        pane.fragment = None;
        Ok(())
    }

    pub fn reattach_frames(&mut self, id: PaneId) -> Result<(), FrameError> {
        {
            let pane = self.panes.get_mut(&id).expect("pane");
            pane.attachment_id = uuid::Uuid::new_v4().to_string();
            pane.live = true;
            pane.control = ControlState::Observe;
            pane.lease_generation = 0;
        }
        self.attach_frames(id)?;
        self.panes.get_mut(&id).expect("pane").control = ControlState::Observe;
        Ok(())
    }

    pub fn drop_daemon_ws(&mut self) {
        self.daemon.set_ws_connected(false);
    }

    pub fn reconnect_daemon_ws(&mut self) -> Result<(), DaemonError> {
        self.daemon.set_ws_connected(true);
        let ids: Vec<PaneId> = self.order.clone();
        for id in ids {
            let pane = self.panes.get_mut(&id).expect("pane");
            pane.live = false;
            pane.control = ControlState::Observe;
            pane.attachment_id = uuid::Uuid::new_v4().to_string();
            pane.live = true;
            let attachment = pane.attachment_id.clone();
            let terminal_id = pane.terminal_id.clone();
            self.daemon.send_ws(json!({
                "type": "terminal_attach",
                "request_id": format!("req-{attachment}"),
                "terminal_id": terminal_id,
                "attachment_id": attachment,
                "frame_delivery": "direct"
            }))?;
        }
        Ok(())
    }

    pub fn respond(&mut self, entry_id: &str, body: Value) -> Result<Value, DaemonError> {
        self.daemon.respond(entry_id, body)
    }

    pub fn spawn_agent(&mut self, body: Value) -> Result<PaneId, FrameError> {
        let result = self
            .daemon
            .spawn_agent(body)
            .map_err(|err| FrameError::Other(err.to_string()))?;
        let terminal_id = result
            .get("terminal_id")
            .and_then(Value::as_str)
            .unwrap_or("term-spawn");
        self.open_terminal(terminal_id, "native", "epoch-a")
    }

    pub fn terminate_terminal(&mut self, terminal_id: &str) -> Result<(), DaemonError> {
        self.daemon.send_ws(json!({
            "type": "terminal_kill",
            "request_id": "req-kill-1",
            "terminal_id": terminal_id
        }))
    }

    pub fn restore_project(&mut self, project_id: &str) -> Result<(), FrameError> {
        self.select_project(project_id);
        let home = self.gobby_home.clone().unwrap_or_default();
        let snapshot: WorkspaceSnapshot =
            load_snapshot(&home, project_id).map_err(|err| FrameError::Other(err.to_string()))?;
        let live = self.daemon.live_terminals().to_vec();
        for terminal_id in snapshot.terminal_ids {
            if live.iter().any(|id| id == &terminal_id) {
                self.open_terminal(&terminal_id, "native", "epoch-a")?;
            }
        }
        self.roster_ids = live;
        Ok(())
    }

    pub fn apply_ws(&mut self, message: &Value) -> Result<(), DaemonError> {
        apply::apply(self, message)
    }
}

impl Workspace {
    pub(super) fn pane_for_attachment_mut(&mut self, attachment_id: &str) -> Option<&mut Pane> {
        self.panes
            .values_mut()
            .find(|pane| pane.attachment_id == attachment_id)
    }

    pub(super) fn remove_terminal(&mut self, terminal_id: &str) {
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
