//! Per-pane attach, lease, and copy-mode state.

use std::collections::HashSet;

use super::attach::AttachState;
use crate::daemon::Generation;
use crate::frame_source::{FrameSource, PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_terminal::protocol::FrameData;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PaneId(pub u32);

/// The chrome's last-resort name for a terminal, used where no pane owns the
/// row yet. A terminal id is a UUID, and four of them truncated into a sidebar
/// are four identical rows, so only the leading segment is worth showing.
pub fn short_terminal_id(terminal_id: &str) -> &str {
    match terminal_id.char_indices().nth(8) {
        Some((split, _)) => &terminal_id[..split],
        None => terminal_id,
    }
}

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
    /// The terminal's own name, as the daemon reports it: a tmux pane title
    /// (`zsh`, `15`) or a spawned agent's session name (`gobby-codex-d0`).
    /// Empty until a roster page arrives, which is what `display_name` covers.
    pub title: String,
    /// The name the user gave this pane in the rename dialog. Roster pages
    /// refresh `title` and leave this alone; `display_name` shows it first.
    pub label: Option<String>,
    /// The terminal's address on its backend — the tmux pane id, `%15`. Unique
    /// and stable where `title` is neither, and it is what the user types into
    /// tmux, so the chrome shows it wherever two terminals could be confused.
    pub address: Option<String>,
    /// The Gobby session running in this terminal, when one is. Attention
    /// entries are keyed by session, not by terminal, so this is what points
    /// a blocked session at the row the user can act on.
    pub session_id: Option<String>,
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
    /// Every right-click in this pane goes to its app instead of the pane
    /// menu (herdr's per-pane passthrough, toggled from that menu).
    pub right_click_passthrough: bool,
    pub(super) frame_source: Option<PaneFrameSource>,
    pub(super) fallback_in_flight: bool,
    pub(super) direct_available: bool,
    pub(super) attach: AttachState,
    pub(super) tombstones: HashSet<String>,
    pub(super) status_message: Option<String>,
    pub(super) terminating: bool,
    pub(super) viewport: (u16, u16),
    /// The viewer the daemon says sizes this terminal, from a refused
    /// `terminal_resize` (decision 14); `None` while gclient's claim stands.
    pub sized_by: Option<String>,
    pub(crate) latest_frame: Option<FrameData>,
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
            title: String::new(),
            label: None,
            address: None,
            session_id: None,
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
            right_click_passthrough: false,
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
            sized_by: None,
            latest_frame: None,
        }
    }

    pub(crate) fn new_detached(
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

    /// What every chrome surface calls this terminal. The user's label first,
    /// then the daemon's title, then the backend address for a row that has
    /// not reported one, then a short id — never the raw UUID, which says
    /// nothing and crowds out the state and backend tokens that share the row.
    pub fn display_name(&self) -> &str {
        if let Some(label) = self.label.as_deref() {
            return label;
        }
        if !self.title.is_empty() {
            return &self.title;
        }
        match self.address.as_deref() {
            Some(address) => address,
            None => short_terminal_id(&self.terminal_id),
        }
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
            AttachState::Attaching { transport, .. } | AttachState::Attached { transport, .. } => {
                Some(transport)
            }
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
        if let AttachState::Attached { transport, .. } = &mut self.attach {
            *transport = source.transport();
        }
        self.frame_source = Some(source);
        self.fallback_in_flight = false;
    }
}
