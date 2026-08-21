use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use super::{color_to_u32, modifier_to_u16, u16_to_modifier, u32_to_color};

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

impl ClientKeyKind {
    #[cfg(any(windows, test))]
    pub(crate) fn from_crossterm(kind: crossterm::event::KeyEventKind) -> Self {
        match kind {
            crossterm::event::KeyEventKind::Press => Self::Press,
            crossterm::event::KeyEventKind::Repeat => Self::Repeat,
            crossterm::event::KeyEventKind::Release => Self::Release,
        }
    }

    pub(crate) fn to_crossterm(self) -> crossterm::event::KeyEventKind {
        match self {
            Self::Press => crossterm::event::KeyEventKind::Press,
            Self::Repeat => crossterm::event::KeyEventKind::Repeat,
            Self::Release => crossterm::event::KeyEventKind::Release,
        }
    }
}

impl ClientKeyCode {
    #[cfg(any(windows, test))]
    pub(crate) fn from_crossterm(code: crossterm::event::KeyCode) -> Option<Self> {
        use crossterm::event::KeyCode;
        Some(match code {
            KeyCode::Backspace => Self::Backspace,
            KeyCode::Enter => Self::Enter,
            KeyCode::Left => Self::Left,
            KeyCode::Right => Self::Right,
            KeyCode::Up => Self::Up,
            KeyCode::Down => Self::Down,
            KeyCode::Home => Self::Home,
            KeyCode::End => Self::End,
            KeyCode::PageUp => Self::PageUp,
            KeyCode::PageDown => Self::PageDown,
            KeyCode::Tab => Self::Tab,
            KeyCode::BackTab => Self::BackTab,
            KeyCode::Delete => Self::Delete,
            KeyCode::Insert => Self::Insert,
            KeyCode::Esc => Self::Esc,
            KeyCode::Char(ch) => Self::Char(ch),
            KeyCode::F(n) => Self::F(n),
            KeyCode::Null => Self::Null,
            _ => return None,
        })
    }

    pub(crate) fn to_crossterm(&self) -> crossterm::event::KeyCode {
        use crossterm::event::KeyCode;
        match self {
            Self::Backspace => KeyCode::Backspace,
            Self::Enter => KeyCode::Enter,
            Self::Left => KeyCode::Left,
            Self::Right => KeyCode::Right,
            Self::Up => KeyCode::Up,
            Self::Down => KeyCode::Down,
            Self::Home => KeyCode::Home,
            Self::End => KeyCode::End,
            Self::PageUp => KeyCode::PageUp,
            Self::PageDown => KeyCode::PageDown,
            Self::Tab => KeyCode::Tab,
            Self::BackTab => KeyCode::BackTab,
            Self::Delete => KeyCode::Delete,
            Self::Insert => KeyCode::Insert,
            Self::Esc => KeyCode::Esc,
            Self::Char(ch) => KeyCode::Char(*ch),
            Self::F(n) => KeyCode::F(*n),
            Self::Null => KeyCode::Null,
        }
    }
}

impl ClientMouseButton {
    #[cfg(any(windows, test))]
    pub(crate) fn from_crossterm(button: crossterm::event::MouseButton) -> Self {
        match button {
            crossterm::event::MouseButton::Left => Self::Left,
            crossterm::event::MouseButton::Right => Self::Right,
            crossterm::event::MouseButton::Middle => Self::Middle,
        }
    }

    pub(crate) fn to_crossterm(self) -> crossterm::event::MouseButton {
        match self {
            Self::Left => crossterm::event::MouseButton::Left,
            Self::Right => crossterm::event::MouseButton::Right,
            Self::Middle => crossterm::event::MouseButton::Middle,
        }
    }
}

impl ClientMouseKind {
    #[cfg(any(windows, test))]
    pub(crate) fn from_crossterm(kind: crossterm::event::MouseEventKind) -> Option<Self> {
        use crossterm::event::MouseEventKind;
        Some(match kind {
            MouseEventKind::Down(button) => Self::Down(ClientMouseButton::from_crossterm(button)),
            MouseEventKind::Up(button) => Self::Up(ClientMouseButton::from_crossterm(button)),
            MouseEventKind::Drag(button) => Self::Drag(ClientMouseButton::from_crossterm(button)),
            MouseEventKind::Moved => Self::Moved,
            MouseEventKind::ScrollUp => Self::ScrollUp,
            MouseEventKind::ScrollDown => Self::ScrollDown,
            MouseEventKind::ScrollLeft => Self::ScrollLeft,
            MouseEventKind::ScrollRight => Self::ScrollRight,
        })
    }

    pub(crate) fn to_crossterm(self) -> crossterm::event::MouseEventKind {
        use crossterm::event::MouseEventKind;
        match self {
            Self::Down(button) => MouseEventKind::Down(button.to_crossterm()),
            Self::Up(button) => MouseEventKind::Up(button.to_crossterm()),
            Self::Drag(button) => MouseEventKind::Drag(button.to_crossterm()),
            Self::Moved => MouseEventKind::Moved,
            Self::ScrollUp => MouseEventKind::ScrollUp,
            Self::ScrollDown => MouseEventKind::ScrollDown,
            Self::ScrollLeft => MouseEventKind::ScrollLeft,
            Self::ScrollRight => MouseEventKind::ScrollRight,
        }
    }
}

impl ClientInputEvent {
    #[cfg(any(windows, test))]
    pub(crate) fn from_crossterm(event: crossterm::event::Event) -> Option<Self> {
        match event {
            crossterm::event::Event::Key(key) => Some(Self::Key {
                code: ClientKeyCode::from_crossterm(key.code)?,
                modifiers: key.modifiers.bits(),
                kind: ClientKeyKind::from_crossterm(key.kind),
                repeat_count: 1,
                generated_text: None,
                source: ClientKeySource::Synthesized,
            }),
            crossterm::event::Event::Mouse(mouse) => Some(Self::Mouse {
                kind: ClientMouseKind::from_crossterm(mouse.kind)?,
                column: mouse.column,
                row: mouse.row,
                modifiers: mouse.modifiers.bits(),
            }),
            crossterm::event::Event::Paste(text) => Some(Self::Paste { text }),
            crossterm::event::Event::FocusGained => Some(Self::FocusGained),
            crossterm::event::Event::FocusLost => Some(Self::FocusLost),
            crossterm::event::Event::Resize(_, _) => None,
        }
    }

    pub(crate) fn to_raw_input_event(&self) -> crate::raw_input::RawInputEvent {
        match self {
            Self::Key {
                code,
                modifiers,
                kind,
                repeat_count,
                generated_text,
                source,
            } => {
                let mut key = crate::input::TerminalKey::new(
                    code.to_crossterm(),
                    crossterm::event::KeyModifiers::from_bits_truncate(*modifiers),
                )
                .with_generated_text(generated_text.clone());
                key = match source {
                    ClientKeySource::Synthesized => key,
                    ClientKeySource::Vt { bytes } => key.with_vt_bytes(bytes.clone()),
                    ClientKeySource::WindowsConsole { record } => key.with_windows_record(*record),
                };
                key = key
                    .with_repeat_count(*repeat_count)
                    .with_kind(kind.to_crossterm());
                crate::raw_input::RawInputEvent::Key(key)
            }
            Self::TextCommit(text) => {
                crate::raw_input::RawInputEvent::Text(crate::input::TextCommit::new(text.clone()))
            }
            Self::Mouse {
                kind,
                column,
                row,
                modifiers,
            } => crate::raw_input::RawInputEvent::Mouse(crossterm::event::MouseEvent {
                kind: kind.to_crossterm(),
                column: *column,
                row: *row,
                modifiers: crossterm::event::KeyModifiers::from_bits_truncate(*modifiers),
            }),
            Self::Paste { text } => crate::raw_input::RawInputEvent::Paste(text.clone()),
            Self::FocusGained => crate::raw_input::RawInputEvent::OuterFocusGained,
            Self::FocusLost => crate::raw_input::RawInputEvent::OuterFocusLost,
        }
    }
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

/// Messages sent from the client to the host over the **read-only** frame socket.
///
/// Variant indices 1–3 are reserved so a hand-built herdr `Input` / `ClipboardImage`
/// / `Resize` payload cannot alias a live verb. The host treats those tags as
/// unknown and never forwards them to a PTY.
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
    /// Legacy herdr Input (tag 1). Rejected as unknown; never a write path.
    LegacyInput {
        data: Vec<u8>,
    },
    /// Legacy herdr ClipboardImage (tag 2). Rejected as unknown.
    LegacyClipboard {
        extension: String,
        data: Vec<u8>,
    },
    /// Legacy herdr Resize (tag 3). Rejected as unknown; never TIOCSWINSZ.
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
    #[cfg(test)]
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
    #[cfg(test)]
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
