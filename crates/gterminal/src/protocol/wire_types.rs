use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use super::{color_to_u32, modifier_to_u16};
use super::{u16_to_modifier, u32_to_color};

/// Render payload encoding negotiated during client handshake.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RenderEncoding {
    /// Send full semantic FrameData values. This is the local/default mode.
    SemanticFrame,
    /// Send already-diffed terminal ANSI byte streams.
    TerminalAnsi,
}

/// Keybinding profile requested by an attached app client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientKeybindings {
    /// Use the server's own keybinding config.
    Server,
    /// Use this attached client's normalized local `[keys]` config.
    Local { keys_toml: String },
}

/// Client behavior requested at connection time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientLaunchMode {
    /// Full app client.
    App,
    /// Direct terminal attach client.
    TerminalAttach,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientKeyKind {
    Press,
    Repeat,
    Release,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientKeyCode {
    Backspace,
    Enter,
    Left,
    Right,
    Up,
    Down,
    Home,
    End,
    PageUp,
    PageDown,
    Tab,
    BackTab,
    Delete,
    Insert,
    Esc,
    Char(char),
    F(u8),
    Null,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientMouseButton {
    Left,
    Right,
    Middle,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientMouseKind {
    Down(ClientMouseButton),
    Up(ClientMouseButton),
    Drag(ClientMouseButton),
    Moved,
    ScrollUp,
    ScrollDown,
    ScrollLeft,
    ScrollRight,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientInputEvent {
    Key {
        code: ClientKeyCode,
        modifiers: u8,
        kind: ClientKeyKind,
        repeat_count: u16,
        generated_text: Option<String>,
        source: ClientKeySource,
    },
    TextCommit(String),
    Mouse {
        kind: ClientMouseKind,
        column: u16,
        row: u16,
        modifiers: u8,
    },
    Paste {
        text: String,
    },
    FocusGained,
    FocusLost,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientKeySource {
    Synthesized,
    Vt {
        bytes: Vec<u8>,
    },
    WindowsConsole {
        record: crate::input::WindowsKeyRecord,
    },
}

/// Client-reported tmux identity used to refuse recursive self-view.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct TmuxClientIdentity {
    pub socket_path: String,
    pub server_pid: i32,
    pub server_start_time: i64,
    pub pane_id: String,
}

/// Physical tmux pane identity carried on user `AttachTerminal`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct PaneLocator {
    pub socket_path: String,
    pub server_pid: i32,
    pub server_start_time: i64,
    pub pane_id: String,
}

impl PaneLocator {
    pub fn locator_key(&self) -> String {
        format!(
            "tmux:{}:{}:{}:{}",
            self.socket_path, self.server_pid, self.server_start_time, self.pane_id
        )
    }

    pub fn matches_identity(&self, identity: &TmuxClientIdentity) -> bool {
        self.socket_path == identity.socket_path
            && self.server_pid == identity.server_pid
            && self.server_start_time == identity.server_start_time
            && self.pane_id == identity.pane_id
    }
}

/// Per-pane mode flags that can change with no cell mutation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct PaneModes {
    pub cursor_visible: bool,
    pub cursor_very_visible: bool,
    pub cursor_shape: u8,
    pub cursor_blinking: bool,
    pub cursor_colour: String,
    pub alternate_on: bool,
    pub keypad_cursor: bool,
    pub keypad: bool,
    pub bracket_paste: bool,
    pub mouse_standard: bool,
    pub mouse_button: bool,
    pub mouse_any: bool,
    pub mouse_all: bool,
    pub mouse_sgr: bool,
    pub mouse_utf8: bool,
    pub wrap: bool,
    pub origin: bool,
    pub insert: bool,
    pub scroll_region_upper: u16,
    pub scroll_region_lower: u16,
    pub pane_in_mode: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseTracking {
    Off,
    X10,
    Normal,
    ButtonMotion,
    AnyMotion,
}

impl PaneModes {
    /// Highest tracking level the app enabled; tmux reports all lower flags too.
    pub fn mouse_tracking(&self) -> MouseTracking {
        if self.mouse_all {
            MouseTracking::AnyMotion
        } else if self.mouse_button {
            MouseTracking::ButtonMotion
        } else if self.mouse_standard {
            MouseTracking::Normal
        } else if self.mouse_any {
            MouseTracking::X10
        } else {
            MouseTracking::Off
        }
    }
}

/// Messages sent from the client to the host over the frame socket. The stream
/// is read-only until the daemon grants it input: `grant_input` names a daemon
/// attachment id on the control socket, the client binds that id with
/// [`ClientMessage::BindAttachment`], and only then do `Input` and `Paste`
/// reach the PTY.
///
/// Variant indices 1–3 are reserved so a hand-built fork-point `Input` /
/// `ClipboardImage` / `Resize` payload cannot alias a live verb. The host
/// treats those tags as unknown and never forwards them to a PTY.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ClientMessage {
    /// Handshake: protocol version, encoding, local CLI token, and viewport.
    Hello {
        version: u32,
        encoding: RenderEncoding,
        local_token: String,
        cols: u16,
        rows: u16,
        #[serde(default)]
        tmux_identity: Option<TmuxClientIdentity>,
    },
    /// Legacy fork-point Input (tag 1). Rejected as unknown; never a write path.
    LegacyInput {
        data: Vec<u8>,
    },
    /// Legacy fork-point ClipboardImage (tag 2). Rejected as unknown.
    LegacyClipboard {
        extension: String,
        data: Vec<u8>,
    },
    /// Legacy fork-point Resize (tag 3). Rejected as unknown; never TIOCSWINSZ.
    LegacyResize {
        cols: u16,
        rows: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    },
    Detach,
    AttachTerminal {
        host_terminal_id: String,
        #[serde(default)]
        reservation_id: Option<String>,
        #[serde(default)]
        locator: Option<PaneLocator>,
    },
    /// Attachment-local render size. Never reaches TIOCSWINSZ.
    SetViewport {
        rows: u16,
        cols: u16,
    },
    /// Attachment-local rows-from-live-edge. Never reaches PTY input.
    SetScrollOffset {
        rows_from_live_edge: u32,
    },
    /// Names the daemon-issued attachment id this stream types as. Input is
    /// delivered only while the slot's `grant_input` holder equals it.
    BindAttachment {
        attachment_id: String,
    },
    /// Raw PTY bytes for the bound attachment.
    Input {
        data: Vec<u8>,
    },
    /// Paste text for the bound attachment; bracketed when the pane asked for it.
    Paste {
        text: String,
    },
}

impl ClientMessage {
    pub fn is_legacy_unknown(&self) -> bool {
        matches!(
            self,
            Self::LegacyInput { .. } | Self::LegacyClipboard { .. } | Self::LegacyResize { .. }
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum AttachScrollDirection {
    Up,
    Down,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum AttachScrollSource {
    Wheel,
    PageKey {
        /// Original key bytes to forward when the child application owns page keys.
        input: Vec<u8>,
    },
}

// ---------------------------------------------------------------------------
// Server → Client messages
// ---------------------------------------------------------------------------

/// A single cell in a rendered frame, serialized independently from ratatui's
/// `Cell` type to keep the wire protocol stable.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CellData {
    /// Grapheme cluster displayed in this cell (usually 1–2 chars).
    pub symbol: String,
    /// Foreground color as a packed u32 (0xAARRGGBB or ratatui Color index).
    pub fg: u32,
    /// Background color as a packed u32.
    pub bg: u32,
    /// Bitmask of style modifiers (bold, italic, etc.) plus Gterm extension bits.
    pub modifier: u16,
    /// Whether this cell should be skipped during diff-based rendering.
    pub skip: bool,
    /// Index into `FrameData::hyperlinks` for this cell's OSC 8 target, if any.
    pub hyperlink: Option<u32>,
}

impl CellData {
    pub(crate) fn from_ratatui_cell(cell: &ratatui::buffer::Cell) -> Self {
        Self {
            symbol: cell.symbol().to_owned(),
            fg: color_to_u32(cell.fg),
            bg: color_to_u32(cell.bg),
            modifier: modifier_to_u16(cell.modifier),
            skip: cell_skip(cell),
            hyperlink: None,
        }
    }
}

fn cell_skip(cell: &ratatui::buffer::Cell) -> bool {
    cell.symbol().is_empty()
}

/// Cursor shape encoded as a DECSCUSR parameter.
///
/// 0 = terminal default, 1 = blinking block, 2 = steady block,
/// 3 = blinking underline, 4 = steady underline, 5 = blinking bar,
/// 6 = steady bar.
pub type CursorShapeParam = u8;

/// Cursor position within a rendered frame.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CursorState {
    /// Column offset (0-based) of the cursor.
    pub x: u16,
    /// Row offset (0-based) of the cursor.
    pub y: u16,
    /// Whether the cursor is visible.
    pub visible: bool,
    /// Cursor shape as a DECSCUSR parameter.
    #[serde(default)]
    pub shape: CursorShapeParam,
}

/// A rendered frame to be displayed by the client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FrameData {
    /// Cells in row-major order. Length must equal `width * height`.
    pub cells: Vec<CellData>,
    /// Frame width in columns.
    pub width: u16,
    /// Frame height in rows.
    pub height: u16,
    /// Cursor state for this frame, if applicable.
    pub cursor: Option<CursorState>,
    /// OSC 8 hyperlink URIs referenced by cells.
    pub hyperlinks: Vec<String>,
    /// Kitty graphics protocol bytes to apply after the text frame.
    pub graphics: Vec<u8>,
    /// Pane mode and cursor appearance flags from a tmux poll or native emulator.
    #[serde(default)]
    pub modes: PaneModes,
}

impl FrameData {
    /// Creates a `FrameData` from a ratatui `Buffer` and optional cursor.
    ///
    /// This converts ratatui's internal cell representation into the
    /// wire-protocol cell format. The conversion is lossless for all
    /// commonly used cell attributes.
    pub fn from_ratatui_buffer(
        buffer: &ratatui::buffer::Buffer,
        cursor: Option<CursorState>,
    ) -> Self {
        Self::from_ratatui_buffer_with_hyperlinks(buffer, cursor, &[])
    }

    pub fn from_ratatui_buffer_with_hyperlinks(
        buffer: &ratatui::buffer::Buffer,
        cursor: Option<CursorState>,
        hyperlinks: &[((u16, u16), String, String)],
    ) -> Self {
        let area = buffer.area;
        let width = area.width;
        let height = area.height;

        let mut hyperlink_uris = Vec::<String>::new();
        let mut hyperlink_indices = HashMap::<&str, u32>::new();
        let mut hyperlink_by_position = HashMap::<(u16, u16), (&str, &str)>::new();
        for ((x, y), symbol, uri) in hyperlinks {
            hyperlink_by_position.insert((*x, *y), (symbol.as_str(), uri.as_str()));
        }
        let mut cells = Vec::with_capacity((width as usize) * (height as usize));
        for row in 0..height {
            for col in 0..width {
                let cell = buffer.cell((col, row)).expect("cell within bounds");
                let hyperlink = hyperlink_by_position
                    .get(&(col, row))
                    .and_then(|(symbol, uri)| {
                        if *symbol != cell.symbol() {
                            return None;
                        }
                        Some(*hyperlink_indices.entry(*uri).or_insert_with(|| {
                            let index = hyperlink_uris.len() as u32;
                            hyperlink_uris.push((*uri).to_owned());
                            index
                        }))
                    });
                let mut cell = CellData::from_ratatui_cell(cell);
                cell.hyperlink = hyperlink;
                cells.push(cell);
            }
        }

        FrameData {
            cells,
            width,
            height,
            cursor,
            hyperlinks: hyperlink_uris,
            graphics: Vec::new(),
            modes: PaneModes::default(),
        }
    }

    /// Reconstructs a ratatui `Buffer` from this frame data.
    ///
    /// Returns `None` if the cells vector length doesn't match `width * height`.
    pub fn to_ratatui_buffer(&self) -> Option<ratatui::buffer::Buffer> {
        let expected = (self.width as usize) * (self.height as usize);
        if self.cells.len() != expected {
            return None;
        }

        let area = ratatui::layout::Rect::new(0, 0, self.width, self.height);
        let mut buffer = ratatui::buffer::Buffer::filled(area, ratatui::buffer::Cell::new(" "));

        for row in 0..self.height {
            for col in 0..self.width {
                let idx = (row as usize) * (self.width as usize) + (col as usize);
                let cell_data = &self.cells[idx];
                let cell = buffer.cell_mut((col, row)).expect("cell within bounds");
                cell.set_symbol(&cell_data.symbol);
                cell.fg = u32_to_color(cell_data.fg);
                cell.bg = u32_to_color(cell_data.bg);
                cell.modifier = u16_to_modifier(cell_data.modifier);
                // reason: ratatui Cell::skip is the current buffer-skip flag on this version.
                #[allow(deprecated)]
                {
                    cell.skip = cell_data.skip;
                }
            }
        }

        Some(buffer)
    }
}

/// Terminal ANSI bytes encoded by the server for network-efficient clients.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TerminalFrame {
    /// Monotonic per-client frame sequence.
    pub seq: u64,
    /// Frame width in columns.
    pub width: u16,
    /// Frame height in rows.
    pub height: u16,
    /// Whether bytes contain a full redraw rather than an incremental diff.
    pub full: bool,
    /// Terminal escape bytes ready to write directly to stdout.
    pub bytes: Vec<u8>,
}

/// Notification kind forwarded from server to client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum NotifyKind {
    /// Play a sound (bell/agent-done, etc.).
    Sound,
    /// Display a toast message through the outer terminal.
    Toast,
    /// Display a toast message through the host OS notification service.
    SystemToast,
}

/// Messages sent from the host to a frame client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ServerMessage {
    Welcome {
        host_epoch: String,
    },
    Frame(FrameData),
    Terminal(TerminalFrame),
    Graphics {
        bytes: Vec<u8>,
    },
    AttachHistory {
        text: String,
        truncated: bool,
        dropped_bytes: u64,
        total_bytes: u64,
    },
    ScrollOffsetApplied {
        applied_rows: u32,
        max_rows: u32,
    },
    TerminalExited {
        host_terminal_id: String,
        #[serde(default)]
        exit_code: Option<i32>,
    },
    Error {
        code: String,
        message: Option<String>,
    },
    Attached {
        created: bool,
        host_terminal_id: String,
    },
    /// Typed refusal of `BindAttachment`, `Input` or `Paste`; the stream stays
    /// open. Codes: `attach_required`, `input_not_granted`, `not_native`,
    /// `request_too_large`, `pty_busy`, `terminal_gone`.
    InputRefused {
        code: String,
    },
}

/// Closed observation-health vocabulary shared with `list` and 3.4.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationState {
    Live,
    Stale,
    OrphanedObservation,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationReason {
    PollSpawnFailed,
    PollTimeout,
    PollPermission,
    PollFdExhausted,
    PollUnparseable,
    GeometryExceedsMaxCells,
    ObservationCeiling,
}

impl ObservationState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Stale => "stale",
            Self::OrphanedObservation => "orphaned_observation",
        }
    }
}

impl ObservationReason {
    pub const LONGEST: &'static str = "geometry_exceeds_max_cells";

    pub fn as_str(self) -> &'static str {
        match self {
            Self::PollSpawnFailed => "poll_spawn_failed",
            Self::PollTimeout => "poll_timeout",
            Self::PollPermission => "poll_permission",
            Self::PollFdExhausted => "poll_fd_exhausted",
            Self::PollUnparseable => "poll_unparseable",
            Self::GeometryExceedsMaxCells => "geometry_exceeds_max_cells",
            Self::ObservationCeiling => "observation_ceiling",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DimensionError {
    ZeroOrNegative,
    AboveMaximum,
    CellProductOverflow,
}

impl DimensionError {
    pub fn code(self) -> &'static str {
        match self {
            Self::ZeroOrNegative => "invalid_dimensions",
            Self::AboveMaximum => "dimensions_too_large",
            Self::CellProductOverflow => "cell_product_overflow",
        }
    }
}

/// Validate rows/cols before any grid allocation or TIOCSWINSZ.
pub fn validate_dimensions(rows: i64, cols: i64) -> Result<(u16, u16), DimensionError> {
    use super::{MAX_CELLS, MAX_COLS, MAX_ROWS, MIN_COLS, MIN_ROWS};
    if rows < i64::from(MIN_ROWS) || cols < i64::from(MIN_COLS) {
        return Err(DimensionError::ZeroOrNegative);
    }
    if rows > i64::from(MAX_ROWS) || cols > i64::from(MAX_COLS) {
        return Err(DimensionError::AboveMaximum);
    }
    let rows_u = rows as u16;
    let cols_u = cols as u16;
    let product = (rows as u64).saturating_mul(cols as u64);
    if product > MAX_CELLS as u64 {
        return Err(DimensionError::CellProductOverflow);
    }
    Ok((rows_u, cols_u))
}
