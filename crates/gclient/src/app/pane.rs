//! Per-pane attach, lease, and copy-mode state.

use std::collections::BTreeMap;

use crate::frame_source::{FrameSource, PaneFrameSource, ScriptedFrameSource, Transport};

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
    pub attachment_id: String,
    pub expected_host_epoch: String,
    pub control: ControlState,
    pub lease_generation: u64,
    pub take_back: bool,
    pub live: bool,
    pub frames_rendered: u32,
    pub scroll_offset: u32,
    pub max_scroll: u32,
    pub new_output: bool,
    pub attach_history: Option<String>,
    pub copy_seeded_from_history: bool,
    pub required_created_flag: bool,
    pub in_flight_write: Option<u64>,
    pub client_write_seq: u64,
    pub bracketed_paste: bool,
    pub search_buffer: String,
    pub copy_search: bool,
    pub fragment: Option<FragmentAcc>,
    pub(super) frame_source: Option<PaneFrameSource>,
    pub(super) fallback_in_flight: bool,
}

#[derive(Debug, Clone, Default)]
pub struct FragmentAcc {
    pub parts: BTreeMap<u32, Vec<u8>>,
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
        Self {
            id,
            terminal_id: terminal_id.into(),
            backend: backend.into(),
            attachment_id: uuid::Uuid::new_v4().to_string(),
            expected_host_epoch: epoch,
            control: ControlState::Observe,
            lease_generation: 0,
            take_back: false,
            live: true,
            frames_rendered: 0,
            scroll_offset: 0,
            max_scroll: 0,
            new_output: false,
            attach_history: None,
            copy_seeded_from_history: false,
            required_created_flag: false,
            in_flight_write: None,
            client_write_seq: 0,
            bracketed_paste: false,
            search_buffer: String::new(),
            copy_search: false,
            fragment: None,
            frame_source: Some(PaneFrameSource::Scripted(frame_source)),
            fallback_in_flight: false,
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
        self.live
    }

    pub fn has_take_back(&self) -> bool {
        self.take_back
    }

    pub fn attachment_id(&self) -> &str {
        &self.attachment_id
    }

    pub fn lease_generation(&self) -> u64 {
        self.lease_generation
    }

    pub fn frames_rendered(&self) -> u32 {
        self.frames_rendered
    }

    pub fn has_fragment_accounting(&self) -> bool {
        self.fragment.is_some()
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
        self.live && self.control == ControlState::Held
    }

    pub fn frame_source(&self) -> Option<&PaneFrameSource> {
        self.frame_source.as_ref()
    }

    pub fn transport(&self) -> Option<Transport> {
        self.frame_source.as_ref().map(FrameSource::transport)
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
