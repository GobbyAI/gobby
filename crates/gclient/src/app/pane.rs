//! Per-pane attach, lease, and copy-mode state.

use std::collections::HashSet;

use super::attach::AttachState;
use crate::daemon::Generation;
use crate::frame_source::{FrameSource, PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_terminal::protocol::FrameData;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PaneId(pub u32);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ControlState {
    Observe,
    Held,
    LeaseLost,
    UncertainReadOnly,
}

#[derive(Debug)]
pub struct Pane {
    pub id: PaneId,
    pub terminal_id: String,
    pub backend: String,
    pub expected_host_epoch: String,
    pub control: ControlState,
    // Chrome consumes this presentation mirror directly. AttachState remains
    // the sole owner of attachment identity, transport, and generations.
    pub live: bool,
    pub take_back: bool,
    pub frames_rendered: u32,
    pub scroll_offset: u32,
    pub max_scroll: u32,
    pub new_output: bool,
    pub attach_history: Option<String>,
    pub copy_seeded_from_history: bool,
    pub required_created_flag: bool,
    pub in_flight_write: Option<u64>,
    pub(super) pending_input: Option<Vec<u8>>,
    pub client_write_seq: u64,
    pub bracketed_paste: bool,
    pub search_buffer: String,
    pub copy_search: bool,
    pub(super) frame_source: Option<PaneFrameSource>,
    pub(super) fallback_in_flight: bool,
    pub(super) direct_available: bool,
    pub(super) attach: AttachState,
    pub(super) tombstones: HashSet<String>,
    pub(super) status_message: Option<String>,
    pub(super) terminating: bool,
    pub(super) viewport: (u16, u16),
    pub(super) latest_frame: Option<FrameData>,
}

impl Pane {
    pub fn new(
        id: PaneId,
        terminal_id: impl Into<String>,
        backend: impl Into<String>,
        epoch: impl Into<String>,
    ) -> Self {
        let epoch = epoch.into();
        let mut frame_source = ScriptedFrameSource::new(Transport::Direct);
        frame_source.set_welcome_epoch(epoch.clone());
        let attachment_id = uuid::Uuid::new_v4().to_string();
        Self {
            id,
            terminal_id: terminal_id.into(),
            backend: backend.into(),
            expected_host_epoch: epoch,
            control: ControlState::Observe,
            live: true,
            take_back: false,
            frames_rendered: 0,
            scroll_offset: 0,
            max_scroll: 0,
            new_output: false,
            attach_history: None,
            copy_seeded_from_history: false,
            required_created_flag: false,
            in_flight_write: None,
            pending_input: None,
            client_write_seq: 0,
            bracketed_paste: false,
            search_buffer: String::new(),
            copy_search: false,
            frame_source: Some(PaneFrameSource::Scripted(frame_source)),
            fallback_in_flight: false,
            direct_available: false,
            attach: AttachState::Attached {
                attachment_id,
                transport: Transport::Direct,
                generation: Generation(0),
                lease_generation: 0,
            },
            tombstones: HashSet::new(),
            status_message: None,
            terminating: false,
            viewport: (24, 80),
            latest_frame: None,
        }
    }

    pub(super) fn new_detached(
        id: PaneId,
        terminal_id: impl Into<String>,
        backend: impl Into<String>,
        epoch: impl Into<String>,
    ) -> Self {
        let mut pane = Self::new(id, terminal_id, backend, epoch);
        pane.frame_source = None;
        pane.attach = AttachState::Detached;
        pane.live = false;
        pane
    }

    pub fn is_observe(&self) -> bool {
        self.control == ControlState::Observe
    }

    pub fn is_held(&self) -> bool {
        self.control == ControlState::Held
    }

    pub fn is_lease_lost(&self) -> bool {
        self.control == ControlState::LeaseLost
    }

    pub fn is_uncertain_readonly(&self) -> bool {
        self.control == ControlState::UncertainReadOnly
    }

    pub fn is_live(&self) -> bool {
        matches!(self.attach, AttachState::Attached { .. })
    }

    pub fn attach_state(&self) -> &AttachState {
        &self.attach
    }

    pub fn has_take_back(&self) -> bool {
        self.take_back
    }

    pub fn attachment_id(&self) -> &str {
        match &self.attach {
            AttachState::Attached { attachment_id, .. } => attachment_id,
            AttachState::Detaching {
                old_attachment_id, ..
            } => old_attachment_id,
            AttachState::Detached | AttachState::Attaching { .. } => "",
        }
    }

    pub fn lease_generation(&self) -> u64 {
        match self.attach {
            AttachState::Attached {
                lease_generation, ..
            } => lease_generation,
            _ => 0,
        }
    }

    pub fn frames_rendered(&self) -> u32 {
        self.frames_rendered
    }

    pub fn in_flight_write(&self) -> Option<u64> {
        self.in_flight_write
    }

    pub fn scroll_offset(&self) -> u32 {
        self.scroll_offset
    }

    pub fn has_new_output(&self) -> bool {
        self.new_output
    }

    pub fn copy_seeded_from_history(&self) -> bool {
        self.copy_seeded_from_history
    }

    pub fn required_created_flag(&self) -> bool {
        self.required_created_flag
    }

    pub fn attach_history(&self) -> Option<&str> {
        self.attach_history.as_deref()
    }

    pub fn search_buffer(&self) -> &str {
        &self.search_buffer
    }

    pub fn writable(&self) -> bool {
        self.is_live() && !self.terminating && self.control == ControlState::Held
    }

    pub fn frame_source(&self) -> Option<&PaneFrameSource> {
        self.frame_source.as_ref()
    }

    pub(super) fn frame_source_mut(&mut self) -> Option<&mut PaneFrameSource> {
        self.frame_source.as_mut()
    }

    pub fn transport(&self) -> Option<Transport> {
        match self.attach {
            AttachState::Attaching { transport, .. }
            | AttachState::Attached { transport, .. } => Some(transport),
            AttachState::Detached | AttachState::Detaching { .. } => {
                self.frame_source.as_ref().map(FrameSource::transport)
            }
        }
    }

    pub fn status_message(&self) -> Option<&str> {
        self.status_message.as_deref()
    }

    pub fn is_terminating(&self) -> bool {
        self.terminating
    }

    pub fn latest_frame(&self) -> Option<&FrameData> {
        self.latest_frame.as_ref()
    }

    pub fn viewport(&self) -> (u16, u16) {
        self.viewport
    }

    pub fn scripted_source(&self) -> Option<&ScriptedFrameSource> {
        self.frame_source
            .as_ref()
            .and_then(PaneFrameSource::scripted)
    }

    pub(super) fn scripted_source_mut(&mut self) -> Option<&mut ScriptedFrameSource> {
        self.frame_source
            .as_mut()
            .and_then(PaneFrameSource::scripted_mut)
    }

    pub(super) fn take_frame_source(&mut self) -> Option<PaneFrameSource> {
        self.frame_source.take()
    }

    pub(super) fn install_frame_source(&mut self, source: PaneFrameSource) {
        self.frame_source = Some(source);
        self.fallback_in_flight = false;
    }
}
