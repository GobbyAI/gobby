//! Pane lookup and frame lifecycle operations on a workspace.

use super::{Pane, PaneId, Workspace};
use crate::daemon::Daemon;
use crate::frame_source::{FrameError, FrameSource, PaneFrameSource};
use gobby_terminal::protocol::ServerMessage;

impl<D: Daemon> Workspace<D> {
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

    pub(super) fn record_source_message(&mut self, id: PaneId, message: &ServerMessage) {
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
            ServerMessage::InputRefused { code } => pane.refuse_host_input(code),
            ServerMessage::ScrollOffsetApplied {
                applied_rows,
                max_rows,
            } => {
                pane.apply_scroll_applied(*applied_rows, *max_rows);
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
        self.relist.invalidate();
        self.pending_spawns.remove(terminal_id);
        let ids: Vec<PaneId> = self
            .order
            .iter()
            .copied()
            .filter(|id| self.panes[id].terminal_id == terminal_id)
            .collect();
        for id in ids {
            // A host-local reconnect for a replaced or closed pane never
            // attaches; the loop cancels its in-flight connect (#23076).
            self.host_recovering.remove(&id);
            self.panes.remove(&id);
            self.host_recovered.remove(&id);
            self.order.retain(|existing| *existing != id);
            if self.focus == Some(id) {
                self.focus = None;
            }
        }
        self.roster_ids.retain(|id| id != terminal_id);
    }
}
