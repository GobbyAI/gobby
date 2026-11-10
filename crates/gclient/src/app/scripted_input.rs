//! The scripted workspace's PTY write paths, split out of `mod.rs` for the
//! 1,000-line ceiling. A native pane on a direct source types straight into
//! its host here, exactly as the live loop's `send_live_write` does, so the
//! harness and the product obey one rule: keystrokes never wait on the daemon
//! (#22573).

use serde_json::json;

use super::attach::frame_daemon_error;
use super::{PaneId, Workspace};
use crate::copy_mode::PASTE_MAX_BYTES;
use crate::daemon::DaemonError;

impl Workspace {
    pub fn send_keys(&mut self, id: PaneId, data: &str) -> Result<(), DaemonError> {
        self.send_input(id, data.as_bytes())
    }

    pub fn send_input(&mut self, id: PaneId, data: &[u8]) -> Result<(), DaemonError> {
        self.ensure_not_exiting()?;
        let daemon = self.ensure_requests_allowed();
        let pane = self.panes.get_mut(&id).expect("pane");
        if !pane.writable() {
            return Err(DaemonError::new(
                409,
                "stale_attachment",
                "attachment is not live",
            ));
        }
        // A native pane types on its host stream with or without the daemon.
        if pane.direct_native() {
            return pane
                .send_host_input(data, false)
                .map_err(frame_daemon_error);
        }
        daemon?;
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
        self.ensure_not_exiting()?;
        if text.len() > PASTE_MAX_BYTES {
            return Err(DaemonError::new(400, "paste_too_large", "paste_too_large"));
        }
        let daemon = self.ensure_requests_allowed();
        let pane = self.panes.get_mut(&id).expect("pane");
        if pane.copy_search {
            pane.search_buffer.push_str(text);
            return Ok(());
        }
        if !pane.writable() {
            return Err(DaemonError::new(
                409,
                "stale_attachment",
                "attachment is not live",
            ));
        }
        // gterm brackets a `Paste` itself, so the host gets the raw text; the
        // daemon path keeps wrapping it on the way to the PTY.
        if pane.direct_native() {
            return pane
                .send_host_input(text.as_bytes(), true)
                .map_err(frame_daemon_error);
        }
        daemon?;
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
