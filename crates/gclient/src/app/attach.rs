//! Attachment lifecycle transitions and request guards.

use super::{ControlState, Pane, PaneId, Workspace};
use crate::daemon::{Daemon, DaemonError, Generation};
use crate::frame_source::{
    FrameError, PaneFrameSource, ScriptedFrameSource, Transport,
};
use gobby_terminal::protocol::ClientMessage;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::Instant;

pub const DETACH_DEADLINE: Duration = Duration::from_secs(2);

#[derive(Debug)]
pub enum AttachState {
    Detached,
    Attaching {
        request_id: String,
        transport: Transport,
        generation: Generation,
    },
    Attached {
        attachment_id: String,
        transport: Transport,
        generation: Generation,
        lease_generation: u64,
    },
    Detaching {
        old_attachment_id: String,
        generation: Generation,
        deadline: Instant,
        supervisor_requested: bool,
    },
}

impl Pane {
    pub(super) fn begin_attaching(
        &mut self,
        request_id: String,
        transport: Transport,
        generation: Generation,
    ) {
        self.attach = AttachState::Attaching {
            request_id,
            transport,
            generation,
        };
        self.live = false;
        self.control = ControlState::Observe;
        self.pending_input = None;
        self.status_message = None;
    }

    pub(super) fn install_attachment(
        &mut self,
        attachment_id: String,
        transport: Transport,
        generation: Generation,
        lease_generation: u64,
        source: PaneFrameSource,
    ) {
        self.tombstones.remove(&attachment_id);
        self.attach = AttachState::Attached {
            attachment_id,
            transport,
            generation,
            lease_generation,
        };
        self.live = true;
        self.control = ControlState::Observe;
        self.take_back = false;
        self.pending_input = None;
        self.status_message = None;
        self.install_frame_source(source);
    }

    pub(super) fn begin_detaching(&mut self, now: Instant) -> Option<(String, Generation)> {
        let (old_attachment_id, generation) = match &self.attach {
            AttachState::Attached {
                attachment_id,
                generation,
                ..
            } => (attachment_id.clone(), *generation),
            AttachState::Detaching {
                old_attachment_id,
                generation,
                ..
            } => return Some((old_attachment_id.clone(), *generation)),
            AttachState::Detached | AttachState::Attaching { .. } => return None,
        };
        self.tombstones.insert(old_attachment_id.clone());
        self.attach = AttachState::Detaching {
            old_attachment_id: old_attachment_id.clone(),
            generation,
            deadline: now + DETACH_DEADLINE,
            supervisor_requested: false,
        };
        self.live = false;
        self.control = ControlState::Observe;
        self.take_back = false;
        self.pending_input = None;
        let _ = self.take_frame_source();
        Some((old_attachment_id, generation))
    }

    pub(super) fn retire_attachment(&mut self, attachment_id: &str, reason: Option<String>) -> bool {
        let matches_current = match &self.attach {
            AttachState::Attached {
                attachment_id: current,
                ..
            } => current == attachment_id,
            AttachState::Detaching {
                old_attachment_id, ..
            } => old_attachment_id == attachment_id,
            AttachState::Attaching { .. } | AttachState::Detached => false,
        };
        if !matches_current {
            return self.tombstones.contains(attachment_id);
        }
        if attachment_id.is_empty() {
            return false;
        }
        self.tombstones.insert(attachment_id.to_string());
        self.attach = AttachState::Detached;
        self.live = false;
        self.control = ControlState::Observe;
        self.take_back = false;
        self.pending_input = None;
        self.in_flight_write = None;
        self.status_message = reason;
        let _ = self.take_frame_source();
        true
    }

    pub(super) fn refuse_attach(&mut self, code: &str, reason: &str) {
        self.attach = AttachState::Detached;
        self.live = false;
        self.control = ControlState::Observe;
        self.take_back = false;
        self.pending_input = None;
        self.in_flight_write = None;
        self.status_message = Some(format!("{code}: {reason}"));
        let _ = self.take_frame_source();
    }

    pub(super) fn clear_control(&mut self, reason: impl Into<String>) {
        self.control = ControlState::Observe;
        self.take_back = false;
        self.pending_input = None;
        self.in_flight_write = None;
        self.status_message = Some(reason.into());
    }

    pub(super) fn set_lease_generation(&mut self, generation: u64) {
        if let AttachState::Attached {
            lease_generation, ..
        } = &mut self.attach
        {
            *lease_generation = generation;
        }
    }

    pub(super) fn detaching_deadline(&self) -> Option<Instant> {
        match self.attach {
            AttachState::Detaching { deadline, .. } => Some(deadline),
            _ => None,
        }
    }

    pub(super) fn take_expired_detach_generation(
        &mut self,
        now: Instant,
    ) -> Option<Generation> {
        if self.detaching_deadline().is_none_or(|deadline| deadline > now) {
            return None;
        }
        let AttachState::Detaching {
            generation,
            supervisor_requested,
            ..
        } = &mut self.attach
        else {
            return None;
        };
        if *supervisor_requested {
            return None;
        }
        *supervisor_requested = true;
        Some(*generation)
    }

    pub(super) fn attached_generation(&self) -> Option<Generation> {
        match self.attach {
            AttachState::Attached { generation, .. }
            | AttachState::Detaching { generation, .. }
            | AttachState::Attaching { generation, .. } => Some(generation),
            AttachState::Detached => None,
        }
    }
}

impl Workspace {
    pub(super) fn advance_scripted_detach(
        &mut self,
        attachment_id: &str,
    ) -> Result<(), DaemonError> {
        let pane_id = self.order.iter().copied().find(|pane_id| {
            matches!(
                self.panes[pane_id].attach_state(),
                AttachState::Detaching {
                    old_attachment_id,
                    ..
                } if old_attachment_id == attachment_id
            )
        });
        let Some(pane_id) = pane_id else {
            return Ok(());
        };
        self.panes
            .get_mut(&pane_id)
            .expect("pane exists")
            .retire_attachment(attachment_id, None);
        self.request_scripted_proxy_attach(pane_id)
    }

    fn request_scripted_proxy_attach(&mut self, pane_id: PaneId) -> Result<(), DaemonError> {
        self.ensure_requests_allowed()?;
        let request_id = uuid::Uuid::new_v4().to_string();
        let generation = Daemon::subscribe(&self.daemon).0.generation;
        let terminal_id = {
            let pane = self.panes.get_mut(&pane_id).expect("pane exists");
            pane.begin_attaching(request_id.clone(), Transport::Proxy, generation);
            pane.terminal_id.clone()
        };
        self.daemon.send_ws(json!({
            "type": "terminal_attach",
            "request_id": request_id,
            "terminal_id": terminal_id,
            "frame_delivery": "proxy",
            "encoding": "semantic_frame"
        }))
    }

    pub(super) fn apply_scripted_attach_result(
        &mut self,
        message: &Value,
    ) -> Result<(), DaemonError> {
        let Some(request_id) = message.get("request_id").and_then(Value::as_str) else {
            return Ok(());
        };
        let pane_id = self.order.iter().copied().find(|pane_id| {
            matches!(
                self.panes[pane_id].attach_state(),
                AttachState::Attaching {
                    request_id: pending,
                    ..
                } if pending == request_id
            )
        });
        let Some(pane_id) = pane_id else {
            return Ok(());
        };
        if message.get("success").and_then(Value::as_bool) != Some(true) {
            let code = message
                .get("code")
                .and_then(Value::as_str)
                .unwrap_or("attach_refused");
            let reason = message
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("terminal attach refused");
            self.panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .refuse_attach(code, reason);
            return Ok(());
        }
        let attachment_id = message
            .get("attachment_id")
            .and_then(Value::as_str)
            .ok_or_else(|| DaemonError::Protocol {
                detail: "attach result omitted attachment_id".into(),
            })?
            .to_string();
        let (transport, generation) = match self.panes[&pane_id].attach_state() {
            AttachState::Attaching {
                transport,
                generation,
                ..
            } => (*transport, *generation),
            _ => return Ok(()),
        };
        if self.panes[&pane_id].tombstones.contains(&attachment_id) {
            self.panes
                .get_mut(&pane_id)
                .expect("pane exists")
                .refuse_attach("attachment_reused", "daemon reused a tombstoned attachment id");
            return Ok(());
        }
        let locator = self.locator_for(pane_id);
        let (rows, cols) = self.panes[&pane_id].viewport();
        let mut source = ScriptedFrameSource::new(transport);
        source.set_welcome_epoch(locator.frame_host_epoch.clone());
        source
            .connect(&locator, cols, rows)
            .map_err(frame_daemon_error)?;
        source
            .send(&ClientMessage::SetViewport { rows, cols })
            .map_err(frame_daemon_error)?;
        let lease_generation = message
            .get("lease_generation")
            .and_then(Value::as_u64)
            .unwrap_or_default();
        self.panes
            .get_mut(&pane_id)
            .expect("pane exists")
            .install_attachment(
                attachment_id,
                transport,
                generation,
                lease_generation,
                PaneFrameSource::Scripted(source),
            );
        Ok(())
    }
}

fn frame_daemon_error(error: FrameError) -> DaemonError {
    DaemonError::Protocol {
        detail: error.to_string(),
    }
}
