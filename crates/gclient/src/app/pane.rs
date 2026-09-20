//! Per-pane attach, lease, and copy-mode state.

use std::collections::HashSet;
use std::fmt;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::time::Instant;

use super::attach::{AttachState, ATTACH_RETRY_BASE};
use crate::daemon::Generation;
use crate::frame_source::{
    FrameError, FrameSource, PaneFrameSource, ScriptedFrameSource, Transport,
};
use gobby_terminal::input::KeyboardProtocol;
use gobby_terminal::protocol::{ClientMessage, FrameData};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PaneId(pub u32);

/// Which runtime owns a terminal: a gclient-native pty or a tmux pane. The
/// daemon reports it as `backend: "native" | "tmux"`; any other word reads as
/// native, the one a pane can least go wrong as.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Backend {
    Tmux,
    #[default]
    #[serde(other)]
    Native,
}

impl Backend {
    pub fn parse(raw: &str) -> Self {
        if raw == "tmux" {
            Self::Tmux
        } else {
            Self::Native
        }
    }

    /// The word the daemon uses for this backend on the wire.
    pub fn wire(self) -> &'static str {
        match self {
            Self::Native => "native",
            Self::Tmux => "tmux",
        }
    }

    /// The word the chrome prints: `gclient` for a native pane, `tmux` for a
    /// tmux one. The wire word `native` names nothing the user can see.
    pub fn label(self) -> &'static str {
        match self {
            Self::Native => "gclient",
            Self::Tmux => "tmux",
        }
    }

    pub fn is_native(self) -> bool {
        matches!(self, Self::Native)
    }
}

impl fmt::Display for Backend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.label())
    }
}

/// What a pane says when the daemon granted the writer lease but the terminal
/// host never got the matching input grant. gclient does not type through the
/// daemon (#22573), so the only honest offer is to take control again.
pub const HOST_GRANT_UNAVAILABLE: &str = "terminal did not grant input; take control again";

/// The last rung of the pane label ladder (D1): what a terminal is called once
/// it has no name of its own and no foreground command the daemon could read.
/// A literal, so no rung of the ladder can be an id.
pub const UNNAMED_PANE: &str = "shell";

/// A UUID reduced to its leading segment, for the surfaces whose subject *is*
/// an id: the destroy-orphans dialog, where a dead row has nothing else left to
/// tell it apart, and machine and session ids with no name yet. Never a pane
/// label — `display_name` is the ladder that keeps ids out of the chrome.
pub fn short_terminal_id(terminal_id: &str) -> &str {
    match terminal_id.char_indices().nth(8) {
        Some((split, _)) => &terminal_id[..split],
        None => terminal_id,
    }
}

/// What the input queue will hold while a grant is in flight. A round trip to
/// a healthy daemon costs a few keys; this is sized for a slow one plus a
/// pasted file, and it exists so a daemon that never answers cannot grow the
/// queue without bound.
const MAX_PENDING_INPUT_BYTES: usize = 256 * 1024;

/// One keystroke or paste waiting for an input grant. Keys and pastes travel
/// as different messages to the terminal host, and a paste that overtook the
/// keys typed before it would reorder what the person wrote, so the queue
/// holds both in one line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PendingInput {
    Keys(Vec<u8>),
    Paste(String),
}

impl PendingInput {
    pub(super) fn new(data: &[u8], paste: bool) -> Self {
        if paste {
            Self::Paste(String::from_utf8_lossy(data).into_owned())
        } else {
            Self::Keys(data.to_vec())
        }
    }

    /// The pair `send_host_input` and the daemon write path both take.
    pub(super) fn parts(&self) -> (&[u8], bool) {
        match self {
            Self::Keys(data) => (data.as_slice(), false),
            Self::Paste(text) => (text.as_bytes(), true),
        }
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
    pub backend: Backend,
    /// The name the user gave this pane in the rename dialog. Roster pages
    /// leave it alone; `display_name` shows it first.
    pub label: Option<String>,
    /// The terminal's address on its backend — the tmux pane id, `%15`. Unique
    /// and stable where a name is neither, and it is what the user types into
    /// tmux, so the chrome shows it wherever two terminals could be confused.
    pub address: Option<String>,
    /// The Gobby session running in this terminal, when one is. Attention
    /// entries are keyed by session, not by terminal, so this is what points
    /// a blocked session at the row the user can act on.
    pub session_id: Option<String>,
    /// The command in this terminal's foreground, as the daemon observed it:
    /// `zsh` at an idle prompt, `nvim` or `cargo` while a job holds it. Rung 2
    /// of the label ladder, and the last rung with any information in it.
    pub command: Option<String>,
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
    /// Keys and pastes typed between a focus change and the input grant that
    /// focus asked for, kept in the order they were typed. A person typing
    /// into a pane they just clicked is not asking to lose the first word
    /// (#22573), so this queue is flushed on the grant rather than dropped.
    pub(super) pending_input: Vec<PendingInput>,
    /// Cached byte count for `pending_input`. Recomputing the sum for every
    /// key turns a stalled 256 KiB queue into quadratic work.
    pending_input_bytes: usize,
    /// The control request this pane is waiting on. The request runs beside
    /// the loop so no click waits on the daemon, which means a reply has to
    /// prove it is still the one this pane wants before it moves any state.
    pub(super) control_request: Option<u64>,
    /// The daemon granted this attachment input at the terminal's host, so its
    /// keystrokes belong on the frame stream rather than in a daemon request
    /// (#22573). Every control result rewrites it; only `Held` panes read it.
    pub host_input_granted: bool,
    /// The attachment id already bound on the installed frame source. gterm
    /// delivers input only to the bound holder, and a fresh source or a fresh
    /// attachment has to bind again.
    pub(super) host_bound_attachment: Option<String>,
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
    /// The daemon lists this terminal with `ownership: external`: a tmux
    /// session someone else started, which closing must never kill.
    pub external: bool,
    pub(super) attach: AttachState,
    /// When the live loop tries a deferred attach again: set by
    /// `defer_attach` after the daemon did not answer or refused for a
    /// reason that clears on its own, cleared when an attach begins.
    pub(super) attach_retry_at: Option<Instant>,
    /// The wait before the next retry; doubles per failure and resets when
    /// an attachment installs.
    pub(super) attach_retry_delay: Duration,
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
        backend: Backend,
        epoch: impl Into<String>,
    ) -> Self {
        let epoch = epoch.into();
        let mut frame_source = ScriptedFrameSource::new(Transport::Direct);
        frame_source.set_welcome_epoch(epoch.clone());
        let attachment_id = uuid::Uuid::new_v4().to_string();
        Self {
            id,
            terminal_id: terminal_id.into(),
            backend,
            label: None,
            address: None,
            session_id: None,
            command: None,
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
            pending_input: Vec::new(),
            pending_input_bytes: 0,
            control_request: None,
            host_input_granted: false,
            host_bound_attachment: None,
            client_write_seq: 0,
            bracketed_paste: false,
            search_buffer: String::new(),
            copy_search: false,
            right_click_passthrough: false,
            frame_source: Some(PaneFrameSource::Scripted(frame_source)),
            fallback_in_flight: false,
            direct_available: false,
            external: false,
            attach: AttachState::Attached {
                attachment_id,
                transport: Transport::Direct,
                generation: Generation(0),
                lease_generation: 0,
            },
            attach_retry_at: None,
            attach_retry_delay: ATTACH_RETRY_BASE,
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
        backend: Backend,
        epoch: impl Into<String>,
    ) -> Self {
        let mut pane = Self::new(id, terminal_id, backend, epoch);
        pane.frame_source = None;
        pane.attach = AttachState::Detached;
        pane.live = false;
        pane
    }

    /// What every chrome surface calls a bare terminal, by the D1 ladder: the
    /// name the user gave the pane, then the command in its foreground, then
    /// the literal `shell`. A terminal running a Gobby session is named by
    /// that session's title on the sidebar instead, so the provider is no
    /// rung here.
    ///
    /// The last rung is a literal so no rung can ever be an id. The daemon's
    /// own `title` is no rung and the client does not keep it: it comes from
    /// `window_name or pane_title or session_name`, which yields `zsh` for one
    /// pane and `75`, `[tmux]` or a whole session banner for the next.
    pub fn display_name(&self) -> &str {
        if let Some(label) = self.label.as_deref().filter(|name| !name.is_empty()) {
            return label;
        }
        if let Some(command) = self.command.as_deref().filter(|name| !name.is_empty()) {
            return command;
        }
        UNNAMED_PANE
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

    /// True while the grant this pane asked for is still in flight. The pane
    /// reads as focused and typing into it queues, so the toggle that gives
    /// control up has to count it as holding (#22573).
    pub fn is_acquiring(&self) -> bool {
        self.control_request.is_some()
    }

    /// The control state shown to a person. Acquisition is intentionally the
    /// normal focused state: input queues until the grant lands, so rendering
    /// it as observe would advertise the wrong interaction contract (#22573).
    pub fn displayed_control(&self) -> ControlState {
        if self.is_acquiring() {
            ControlState::Held
        } else {
            self.control
        }
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

    /// Take a `ScrollOffsetApplied`, keeping the confirmed ceiling when the
    /// reply is only the daemon's echo.
    ///
    /// A request that carries no ceiling comes back from the daemon as
    /// `{applied_rows: n, max_rows: n}` before gterm's real depth is relayed,
    /// and that echo can land after the relay just as easily as before it.
    /// Adopting it would shrink `max_scroll` to wherever the pane already
    /// sits, so the next notch would clamp to its own position and send
    /// nothing — and the request that would re-learn the depth is the one that
    /// clamp suppresses. A ceiling equal to its own applied rows that is
    /// unknown or below the confirmed one is that echo, not a limit; the same
    /// rule the web applies in `useTerminalScrollOffset`.
    pub fn apply_scroll_applied(&mut self, applied: u32, max_rows: u32) {
        let echo = max_rows == applied && (self.max_scroll == 0 || max_rows < self.max_scroll);
        if !echo {
            self.max_scroll = max_rows;
        }
        self.scroll_offset = applied;
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

    /// Frames arrive on a direct socket and the backend is one gclient can
    /// write to. Whether it may type is [`Pane::direct_input`].
    fn direct_native(&self) -> bool {
        self.transport() == Some(Transport::Direct) && self.backend.is_native()
    }

    /// This pane types straight into its terminal's host: a direct frame
    /// socket, a native backend, and a daemon-issued input grant (#22573).
    pub fn direct_input(&self) -> bool {
        self.direct_native() && self.host_input_granted
    }

    /// Type `data` into the host on the frame stream, binding this attachment
    /// the first time. Never awaits: a full write channel drops the key and
    /// names the backlog on the pane instead of stalling the render loop.
    /// Holds one key or paste until the grant focus asked for lands. `false`
    /// means the queue is full and the caller has to say so: swallowing it
    /// here would be the dropped keystroke this queue exists to prevent.
    pub(super) fn queue_input(&mut self, data: &[u8], paste: bool) -> bool {
        if self.pending_input_bytes.saturating_add(data.len()) > MAX_PENDING_INPUT_BYTES {
            return false;
        }
        self.pending_input.push(PendingInput::new(data, paste));
        self.pending_input_bytes += data.len();
        true
    }

    pub(super) fn take_pending_input(&mut self) -> Vec<PendingInput> {
        self.pending_input_bytes = 0;
        std::mem::take(&mut self.pending_input)
    }

    pub(super) fn clear_pending_input(&mut self) {
        self.pending_input.clear();
        self.pending_input_bytes = 0;
    }

    pub(super) fn send_host_input(&mut self, data: &[u8], paste: bool) -> Result<(), FrameError> {
        let attachment_id = self.attachment_id().to_string();
        let needs_bind = self.host_bound_attachment.as_deref() != Some(attachment_id.as_str());
        let message = if paste {
            ClientMessage::Paste {
                text: String::from_utf8_lossy(data).into_owned(),
            }
        } else {
            ClientMessage::Input {
                data: data.to_vec(),
            }
        };
        let source = self
            .frame_source
            .as_mut()
            .ok_or_else(|| FrameError::Protocol("pane has no frame source".into()))?;
        if needs_bind {
            source.send_input(&ClientMessage::BindAttachment {
                attachment_id: attachment_id.clone(),
            })?;
        }
        let outcome = source.send_input(&message);
        if needs_bind {
            self.host_bound_attachment = Some(attachment_id);
        }
        match outcome {
            Err(error @ FrameError::Backpressure) => {
                self.status_message = Some(error.to_string());
                Ok(())
            }
            other => other,
        }
    }

    /// Record the host input grant that came with a granted writer lease. A
    /// direct native pane the host did not grant cannot type there, and
    /// gclient never falls back to daemon-mediated keys, so the pane returns
    /// to observing and offers take-back. Reports whether the grant stands.
    pub(super) fn apply_host_grant(&mut self, granted: Option<bool>) -> bool {
        self.host_input_granted = granted.unwrap_or(false);
        if self.host_input_granted || !self.direct_native() {
            return true;
        }
        self.control = ControlState::Observe;
        self.take_back = true;
        self.clear_pending_input();
        self.status_message = Some(HOST_GRANT_UNAVAILABLE.to_string());
        false
    }

    /// gterm refused a host write: `input_not_granted` once the daemon moved
    /// the grant, `terminal_gone` once the PTY went away. The stream stays
    /// open; only this pane's claim to type on it is gone.
    pub(super) fn refuse_host_input(&mut self, code: &str) {
        self.host_input_granted = false;
        self.control = ControlState::Observe;
        self.take_back = true;
        self.clear_pending_input();
        self.status_message = Some(format!(
            "terminal refused input ({code}); take control again"
        ));
    }

    pub fn frame_source(&self) -> Option<&PaneFrameSource> {
        self.frame_source.as_ref()
    }

    pub async fn request_text(
        &mut self,
        start_rows_from_live_edge: u32,
        start_col: u16,
        end_rows_from_live_edge: u32,
        end_col: u16,
    ) -> Result<(), FrameError> {
        let source = self
            .frame_source
            .as_mut()
            .ok_or_else(|| FrameError::Other("pane has no frame source".into()))?;
        source
            .send(&ClientMessage::ReadText {
                start_rows_from_live_edge,
                start_col,
                end_rows_from_live_edge,
                end_col,
            })
            .await
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

    pub fn keyboard_protocol(&self) -> KeyboardProtocol {
        KeyboardProtocol::from_kitty_flags(
            self.latest_frame()
                .map_or(0, |frame| frame.modes.kitty_keyboard_flags),
        )
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
        self.host_bound_attachment = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pane() -> Pane {
        Pane::new_detached(PaneId(1), "terminal-1", Backend::Native, "epoch-1")
    }

    #[test]
    fn acquiring_control_displays_as_held() {
        let mut pane = pane();
        pane.control = ControlState::Observe;
        pane.control_request = Some(1);

        assert_eq!(pane.displayed_control(), ControlState::Held);
    }

    #[test]
    fn pending_input_cap_resets_when_the_queue_is_cleared() {
        let mut pane = pane();
        let full = vec![b'x'; MAX_PENDING_INPUT_BYTES];

        assert!(pane.queue_input(&full, false));
        assert!(!pane.queue_input(b"x", false));
        pane.clear_pending_input();
        assert!(pane.queue_input(b"x", false));
    }
}
