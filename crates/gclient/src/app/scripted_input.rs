//! The scripted workspace's PTY write paths, split out of `mod.rs` for the
//! 1,000-line ceiling. A held native pane on a direct source types straight
//! into its host here, exactly as the live loop's `send_live_write` does, so
//! the harness and the product obey one rule: keystrokes never wait on the
//! daemon (#22573).

use serde_json::json;

use super::attach::frame_daemon_error;
use super::{ControlState, PaneId, Workspace};
use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::DaemonError;

impl Workspace {
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
        if pane.direct_input() {
            return pane
                .send_host_input(data, false)
                .map_err(frame_daemon_error);
        }
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
        // gterm brackets a granted `Paste` itself, so the host gets the raw
        // text; the daemon path keeps wrapping it on the way to the PTY.
        if pane.direct_input() {
            return pane
                .send_host_input(text.as_bytes(), true)
                .map_err(frame_daemon_error);
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
}
