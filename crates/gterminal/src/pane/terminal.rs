use std::borrow::Cow;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use ratatui::style::{Color, Modifier, Style};
use ratatui::{layout::Rect, Frame};
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;
use tracing::{debug, error};
use unicode_width::UnicodeWidthStr;

use crate::layout::PaneId;
use crate::protocol::CellData;

#[cfg(windows)]
mod windows_recent_fallback;

use super::cursor::{CursorPositionSettleState, DecscusrTracker, CURSOR_POSITION_SETTLE};
use super::{
    input::{
        ghostty_key_event_from_terminal_key, ghostty_mouse_encoder_for_terminal,
        ghostty_mouse_event_from_button_kind, ghostty_mouse_event_from_motion_kind,
        ghostty_mouse_event_from_wheel_kind, ghostty_prefers_gterm_text_encoding,
    },
    kitty_keyboard::KittyKeyboardTracker,
    osc::{
        contains_scrollback_clear_sequence, current_transient_default_color_owner,
        maybe_filter_primary_screen_scrollback_clear, parse_reported_cwd,
        restore_host_terminal_theme_if_needed, write_host_terminal_theme_selective,
        AgentOscStateTracker, DefaultColorEvent, DefaultColorEventTracker, DefaultColorOscTracker,
        DefaultColorQuery, DefaultColorTrackedEvent, OscDebugTracker,
    },
    xtgettcap::{XtgettcapQueryTracker, XtgettcapResponse},
};

const DEFAULT_DETECTION_ROWS: usize = 24;
const KITTY_GRAPHICS_REDRAW_SETTLE: Duration = Duration::from_millis(20);
const CURSOR_POSITION_SETTLE_ENABLED: bool = cfg!(windows);
const MODE_MOUSE_X10: u16 = 9;
const MODE_MOUSE_PRESS_RELEASE: u16 = 1000;
const MODE_MOUSE_BUTTON_MOTION: u16 = 1002;
const MODE_MOUSE_ANY_MOTION: u16 = 1003;

pub use crate::layout::ScrollMetrics;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct TerminalTextPoint {
    pub row: u32,
    pub col: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct TerminalTextMatch {
    pub start: TerminalTextPoint,
    pub end: TerminalTextPoint,
    pub source_fingerprint: u64,
    pub scan_cols: u16,
    pub scan_screen: crate::ghostty::ActiveScreen,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TerminalWordMotion {
    NextStart,
    PreviousStart,
    NextEnd,
}

const COPY_MODE_WORD_SEPARATORS: &str = "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TerminalCursorState {
    pub x: u16,
    pub y: u16,
    pub visible: bool,
    /// DECSCUSR parameter (0–6). 0 means terminal default.
    pub shape: u8,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TerminalDirtyPatch {
    pub rows: Vec<(u16, Vec<CellData>)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum TerminalDirtyPatchOutcome {
    Clean,
    Patch(TerminalDirtyPatch),
    Fallback,
}

fn decscusr_cursor_shape(style: crate::ghostty::CursorVisualStyle, blinking: bool) -> u8 {
    match (style, blinking) {
        (crate::ghostty::CursorVisualStyle::Block, true)
        | (crate::ghostty::CursorVisualStyle::BlockHollow, true) => 1,
        (crate::ghostty::CursorVisualStyle::Block, false)
        | (crate::ghostty::CursorVisualStyle::BlockHollow, false) => 2,
        (crate::ghostty::CursorVisualStyle::Underline, true) => 3,
        (crate::ghostty::CursorVisualStyle::Underline, false) => 4,
        (crate::ghostty::CursorVisualStyle::Bar, true) => 5,
        (crate::ghostty::CursorVisualStyle::Bar, false) => 6,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct InputState {
    pub alternate_screen: bool,
    pub application_cursor: bool,
    pub bracketed_paste: bool,
    pub focus_reporting: bool,
    pub mouse_protocol_mode: crate::input::MouseProtocolMode,
    pub mouse_protocol_encoding: crate::input::MouseProtocolEncoding,
    pub mouse_alternate_scroll: bool,
    #[serde(default)]
    pub modify_other_keys: bool,
    #[serde(default)]
    pub color_scheme_reporting: bool,
}

impl InputState {
    pub fn mouse_reporting_enabled(self) -> bool {
        self.mouse_protocol_mode.reporting_enabled()
    }

    pub fn plain_page_keys_use_host_scrollback(self) -> bool {
        !self.alternate_screen
            && !self.mouse_reporting_enabled()
            // Bracketed paste distinguishes zsh's line editor (where it's on)
            // from e.g. less -X (where it's off).
            && (!self.application_cursor || self.bracketed_paste)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ProcessBytesResult {
    pub request_render: bool,
    pub render_delay: Option<Duration>,
    pub clipboard_writes: Vec<Vec<u8>>,
    pub reported_cwd: Option<std::path::PathBuf>,
    pub terminal_responses: Vec<Bytes>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub(crate) struct TerminalReadSnapshot {
    pub text: String,
    pub truncated: bool,
}

pub(crate) struct GhosttyPaneTerminal {
    pub core: Mutex<GhosttyPaneCore>,
    key_encoder: Mutex<crate::ghostty::KeyEncoder>,
    pending_pty_responses: Arc<Mutex<Vec<Bytes>>>,
}

pub(crate) struct GhosttyPaneCore {
    pub terminal: crate::ghostty::Terminal,
    #[cfg(windows)]
    recent_fallback: windows_recent_fallback::Cache,
    pub render_state: crate::ghostty::RenderState,
    pub kitty_keyboard: KittyKeyboardTracker,
    pub initial_default_foreground: Option<crate::ghostty::RgbColor>,
    pub initial_default_background: Option<crate::ghostty::RgbColor>,
    pub host_terminal_theme: crate::terminal_theme::TerminalTheme,
    pub transient_default_color_owner_pgid: Option<u32>,
    pub default_color_tracker: DefaultColorOscTracker,
    pub default_color_event_tracker: DefaultColorEventTracker,
    pub child_default_foreground_changed: bool,
    pub child_default_background_changed: bool,
    pub osc_debug_tracker: OscDebugTracker,
    pub agent_osc_state: AgentOscStateTracker,
    pub xtgettcap_query_tracker: XtgettcapQueryTracker,
    decscusr_tracker: DecscusrTracker,
    cursor_settle_state: CursorPositionSettleState,
    windows_powershell_prompt_cwd_reporting: bool,
}

pub(crate) struct PaneTerminal {
    pub(crate) ghostty: GhosttyPaneTerminal,
}

impl PaneTerminal {
    pub(crate) fn new(ghostty: GhosttyPaneTerminal) -> Self {
        Self { ghostty }
    }

    pub fn process_pty_bytes(
        &self,
        pane_id: PaneId,
        shell_pid: u32,
        bytes: &[u8],
        response_writer: &mpsc::Sender<Bytes>,
    ) -> ProcessBytesResult {
        self.ghostty
            .process_pty_bytes(pane_id, shell_pid, bytes, response_writer)
    }

    pub fn resize(
        &self,
        rows: u16,
        cols: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    ) -> Vec<Bytes> {
        self.ghostty
            .resize(rows, cols, cell_width_px, cell_height_px)
    }

    pub fn scroll_up(&self, lines: usize) {
        self.ghostty.scroll_up(lines);
    }

    pub fn scroll_down(&self, lines: usize) {
        self.ghostty.scroll_down(lines);
    }

    pub fn scroll_reset(&self) {
        self.ghostty.scroll_reset();
    }

    pub fn set_scroll_offset_from_bottom(&self, lines: usize) {
        self.ghostty.set_scroll_offset_from_bottom(lines);
    }

    pub fn scroll_metrics(&self) -> Option<ScrollMetrics> {
        self.ghostty.scroll_metrics()
    }

    pub(crate) fn search_text_matches(
        &self,
        query: &str,
        case_sensitive: bool,
    ) -> Vec<TerminalTextMatch> {
        let Some((buffer, active_screen)) = self.retained_text_buffer() else {
            return Vec::new();
        };
        buffer.search(query, case_sensitive, active_screen)
    }

    pub(crate) fn text_match_is_current(&self, text_match: TerminalTextMatch) -> bool {
        self.text_matches_are_current(&[text_match])
            .first()
            .copied()
            .unwrap_or(false)
    }

    pub(crate) fn text_matches_are_current(&self, text_matches: &[TerminalTextMatch]) -> Vec<bool> {
        if text_matches.is_empty() {
            return Vec::new();
        }
        let Ok(core) = self.ghostty.core.lock() else {
            return vec![false; text_matches.len()];
        };
        let Some(cols) = core.terminal.cols().ok() else {
            return vec![false; text_matches.len()];
        };
        let Some(active_screen) = core.terminal.active_screen().ok() else {
            return vec![false; text_matches.len()];
        };
        let row_range = text_matches
            .iter()
            .filter(|text_match| {
                text_match.scan_cols == cols && text_match.scan_screen == active_screen
            })
            .fold(None::<(u32, u32)>, |range, text_match| {
                Some(match range {
                    Some((start_row, end_row)) => (
                        start_row.min(text_match.start.row),
                        end_row.max(text_match.end.row),
                    ),
                    None => (text_match.start.row, text_match.end.row),
                })
            });
        let Some((start_row, end_row)) = row_range else {
            return vec![false; text_matches.len()];
        };
        let Ok(rows) = core
            .terminal
            .screen_text_rows_range(start_row as usize, end_row.saturating_add(1) as usize)
        else {
            return vec![false; text_matches.len()];
        };
        let buffer = RetainedTextBuffer::new_search(cols, rows, start_row);
        text_matches
            .iter()
            .map(|text_match| {
                text_match.scan_cols == cols
                    && text_match.scan_screen == active_screen
                    && buffer.contains_match(*text_match)
            })
            .collect()
    }

    pub(crate) fn word_motion_target(
        &self,
        row: u32,
        col: u16,
        motion: TerminalWordMotion,
    ) -> Option<TerminalTextPoint> {
        let core = self.ghostty.core.lock().ok()?;
        let cols = core.terminal.cols().ok()?;
        let total_rows = core.terminal.total_rows().ok()?;
        let row = usize::try_from(row).ok()?;
        if row >= total_rows {
            return None;
        }

        let mut window_rows = 64usize;
        loop {
            let (start_row, end_row) = match motion {
                TerminalWordMotion::PreviousStart => {
                    (row.saturating_sub(window_rows.saturating_sub(1)), row + 1)
                }
                TerminalWordMotion::NextStart | TerminalWordMotion::NextEnd => {
                    (row, row.saturating_add(window_rows).min(total_rows))
                }
            };
            let rows = core
                .terminal
                .screen_text_rows_range(start_row, end_row)
                .ok()?;
            let starts_in_continuation = rows
                .first()
                .is_some_and(|row| row.wrap_continuation && start_row > 0);
            let ends_in_continuation = rows
                .last()
                .is_some_and(|row| row.soft_wrapped && end_row < total_rows);
            let buffer = RetainedTextBuffer::new_words(cols, rows, u32::try_from(start_row).ok()?);
            let target = buffer.word_motion(u32::try_from(row).ok()?, col, motion);
            let needs_more_history = motion == TerminalWordMotion::PreviousStart
                && target
                    .is_some_and(|target| starts_in_continuation && target.row == start_row as u32);
            let needs_more_future = motion == TerminalWordMotion::NextEnd
                && ends_in_continuation
                && target.is_some_and(|target| buffer.point_is_final_atom(target));
            if target.is_some() && !needs_more_history && !needs_more_future {
                return target;
            }

            let reached_edge = match motion {
                TerminalWordMotion::PreviousStart => start_row == 0,
                TerminalWordMotion::NextStart | TerminalWordMotion::NextEnd => {
                    end_row == total_rows
                }
            };
            if reached_edge {
                return target;
            }
            window_rows = window_rows.saturating_mul(2).min(total_rows);
        }
    }

    fn retained_text_buffer(&self) -> Option<(RetainedTextBuffer, crate::ghostty::ActiveScreen)> {
        let (cols, rows, active_screen) = {
            let core = self.ghostty.core.lock().ok()?;
            let cols = core.terminal.cols().ok()?;
            let rows = core.terminal.screen_text_rows().ok()?;
            let active_screen = core.terminal.active_screen().ok()?;
            (cols, rows, active_screen)
        };
        Some((RetainedTextBuffer::new_search(cols, rows, 0), active_screen))
    }

    pub fn input_state(&self) -> Option<InputState> {
        self.ghostty.input_state()
    }

    pub fn wheel_routing(&self) -> Option<crate::pane::WheelRouting> {
        self.ghostty.wheel_routing()
    }

    pub(crate) fn screen_text_snapshot(
        &self,
    ) -> Option<(
        crate::ghostty::ActiveScreen,
        u16,
        Vec<crate::ghostty::ScreenTextRow>,
    )> {
        self.ghostty.screen_text_snapshot()
    }

    pub fn cursor_state(&self) -> Option<TerminalCursorState> {
        self.ghostty.cursor_state()
    }

    pub fn synchronized_output_active(&self) -> bool {
        self.ghostty.synchronized_output_active()
    }

    pub fn visible_text(&self) -> String {
        self.ghostty.visible_text()
    }

    pub fn visible_ansi(&self) -> String {
        self.ghostty.visible_ansi()
    }

    pub fn detection_text(&self) -> String {
        self.ghostty.detection_text()
    }

    pub fn recent_text(&self, lines: usize) -> String {
        self.ghostty.recent_text(lines)
    }

    pub(crate) fn recent_text_snapshot(&self, lines: usize) -> TerminalReadSnapshot {
        self.ghostty.recent_text_snapshot(lines)
    }

    pub(crate) fn recent_ansi_snapshot(&self, lines: usize) -> TerminalReadSnapshot {
        self.ghostty.recent_ansi_snapshot(lines)
    }

    pub(crate) fn recent_unwrapped_text_snapshot(&self, lines: usize) -> TerminalReadSnapshot {
        self.ghostty.recent_unwrapped_text_snapshot(lines)
    }

    pub fn recent_unwrapped_ansi(&self, lines: usize) -> String {
        self.ghostty.recent_unwrapped_ansi(lines)
    }

    pub(crate) fn recent_unwrapped_ansi_snapshot(&self, lines: usize) -> TerminalReadSnapshot {
        self.ghostty.recent_unwrapped_ansi_snapshot(lines)
    }

    pub fn extract_selection(&self, selection: &crate::selection::Selection) -> Option<String> {
        self.ghostty.extract_selection(selection)
    }

    pub fn render(&self, frame: &mut Frame, area: Rect, show_cursor: bool) {
        self.ghostty.render(frame, area, show_cursor);
    }

    pub fn collect_dirty_patch(
        &self,
        area_width: u16,
        area_height: u16,
    ) -> TerminalDirtyPatchOutcome {
        self.ghostty.collect_dirty_patch(area_width, area_height)
    }

    pub fn visible_hyperlinks(&self, area: Rect) -> Vec<((u16, u16), String, String)> {
        self.ghostty.visible_hyperlinks(area)
    }

    pub fn kitty_image_placements_with_data_filter<F>(
        &self,
        needs_data: F,
    ) -> Vec<crate::ghostty::KittyImagePlacement>
    where
        F: FnMut(crate::ghostty::KittyImageDescriptor) -> bool,
    {
        self.ghostty
            .kitty_image_placements_with_data_filter(needs_data)
    }

    pub fn apply_host_terminal_theme(&self, theme: crate::terminal_theme::TerminalTheme) {
        self.ghostty.apply_host_terminal_theme(theme);
    }

    pub fn apply_host_terminal_appearance(
        &self,
        appearance: Option<crate::terminal_theme::HostAppearance>,
    ) -> Option<Bytes> {
        self.ghostty.apply_host_terminal_appearance(appearance)
    }

    pub fn has_transient_default_color_override(&self) -> bool {
        self.ghostty.has_transient_default_color_override()
    }

    pub fn maybe_restore_host_terminal_theme(&self, pane_id: PaneId, shell_pid: u32) -> bool {
        self.ghostty
            .maybe_restore_host_terminal_theme(pane_id, shell_pid)
    }

    pub fn terminal_title(&self) -> Option<String> {
        self.ghostty.terminal_title()
    }

    #[allow(dead_code)] // exposed for Stage C (detection loop wiring)
    pub fn agent_osc_title(&self) -> String {
        self.ghostty.agent_osc_title()
    }

    #[allow(dead_code)] // exposed for Stage C (detection loop wiring)
    pub fn agent_osc_progress(&self) -> String {
        self.ghostty.agent_osc_progress()
    }

    /// Clears retained OSC title/progress evidence on foreground agent change.
    pub fn clear_agent_osc_state(&self) {
        self.ghostty.clear_agent_osc_state()
    }

    pub fn keyboard_protocol(
        &self,
        fallback: crate::input::KeyboardProtocol,
    ) -> crate::input::KeyboardProtocol {
        self.ghostty.keyboard_protocol().unwrap_or(fallback)
    }

    #[cfg(unix)]
    pub fn kitty_keyboard_state_ansi(&self) -> Option<String> {
        self.ghostty
            .kitty_keyboard_state_ansi()
            .filter(|ansi| !ansi.is_empty())
    }

    pub fn encode_terminal_key(
        &self,
        key: crate::input::TerminalKey,
        protocol: crate::input::KeyboardProtocol,
    ) -> Vec<u8> {
        self.ghostty.encode_terminal_key(key, protocol)
    }

    pub fn encode_mouse_button(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        self.ghostty
            .encode_mouse_button(kind, column, row, modifiers)
    }

    pub fn encode_mouse_motion(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        self.ghostty
            .encode_mouse_motion(kind, column, row, modifiers)
    }

    pub fn encode_mouse_wheel(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        self.ghostty
            .encode_mouse_wheel(kind, column, row, modifiers)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TextClass {
    Whitespace,
    Separator,
    Word,
}

#[derive(Debug)]
struct TextAtom {
    point: Option<TerminalTextPoint>,
    end_col: u16,
    class: TextClass,
}

#[derive(Debug)]
struct TextSpan {
    byte_start: usize,
    byte_end: usize,
    start: TerminalTextPoint,
    end: TerminalTextPoint,
}

#[derive(Debug, Default)]
struct LogicalTextLine {
    text: String,
    spans: Vec<TextSpan>,
}

#[derive(Debug)]
struct RetainedTextBuffer {
    cols: u16,
    lines: Vec<LogicalTextLine>,
    atoms: Vec<TextAtom>,
}

impl RetainedTextBuffer {
    #[cfg(test)]
    fn new(cols: u16, rows: Vec<crate::ghostty::ScreenTextRow>) -> Self {
        Self::build(cols, rows, 0, true, true)
    }

    fn new_search(cols: u16, rows: Vec<crate::ghostty::ScreenTextRow>, row_offset: u32) -> Self {
        Self::build(cols, rows, row_offset, true, false)
    }

    fn new_words(cols: u16, rows: Vec<crate::ghostty::ScreenTextRow>, row_offset: u32) -> Self {
        Self::build(cols, rows, row_offset, false, true)
    }

    fn build(
        cols: u16,
        rows: Vec<crate::ghostty::ScreenTextRow>,
        row_offset: u32,
        build_lines: bool,
        build_atoms: bool,
    ) -> Self {
        let mut lines = Vec::new();
        let mut line = LogicalTextLine::default();
        let mut atoms: Vec<TextAtom> = Vec::new();

        for (row_idx, row) in rows.into_iter().enumerate() {
            let Some(row_idx) = u32::try_from(row_idx).ok() else {
                break;
            };
            let row_idx = row_offset.saturating_add(row_idx);
            for (col, cell) in row.cells.into_iter().enumerate() {
                let Ok(col) = u16::try_from(col) else {
                    break;
                };
                if cell.wide == crate::ghostty::CellWide::SpacerTail {
                    continue;
                }
                if cell.wide == crate::ghostty::CellWide::SpacerHead {
                    if build_atoms {
                        atoms.push(TextAtom {
                            point: Some(TerminalTextPoint { row: row_idx, col }),
                            end_col: col,
                            class: atoms
                                .last()
                                .map_or(TextClass::Whitespace, |atom| atom.class),
                        });
                    }
                    continue;
                }
                let width = if cell.wide == crate::ghostty::CellWide::Wide {
                    2
                } else {
                    1
                };
                let text = terminal_cell_text(&cell.graphemes);
                let start = TerminalTextPoint { row: row_idx, col };
                let end = TerminalTextPoint {
                    row: row_idx,
                    col: col.saturating_add(width - 1),
                };
                if build_lines {
                    let byte_start = line.text.len();
                    line.text.push_str(&text);
                    let byte_end = line.text.len();
                    line.spans.push(TextSpan {
                        byte_start,
                        byte_end,
                        start,
                        end,
                    });
                }
                if build_atoms {
                    atoms.push(TextAtom {
                        point: Some(start),
                        end_col: end.col,
                        class: text_class(&text),
                    });
                }
            }

            if row.soft_wrapped {
                continue;
            }

            if build_lines {
                let trimmed_len = line.text.trim_end().len();
                while line
                    .spans
                    .last()
                    .is_some_and(|span| span.byte_start >= trimmed_len)
                {
                    line.spans.pop();
                }
                line.text.truncate(trimmed_len);
                lines.push(std::mem::take(&mut line));
            }
            if build_atoms {
                atoms.push(TextAtom {
                    point: None,
                    end_col: 0,
                    class: TextClass::Whitespace,
                });
            }
        }

        if build_lines && (!line.text.is_empty() || !line.spans.is_empty()) {
            lines.push(line);
        }

        Self { cols, lines, atoms }
    }

    fn search(
        &self,
        query: &str,
        case_sensitive: bool,
        active_screen: crate::ghostty::ActiveScreen,
    ) -> Vec<TerminalTextMatch> {
        if query.is_empty() {
            return Vec::new();
        }
        let Ok(regex) = regex::RegexBuilder::new(&regex::escape(query))
            .case_insensitive(!case_sensitive)
            .build()
        else {
            return Vec::new();
        };
        let mut matches = Vec::new();
        for line in &self.lines {
            for found in regex.find_iter(&line.text) {
                let Ok(start_index) = line
                    .spans
                    .binary_search_by_key(&found.start(), |span| span.byte_start)
                else {
                    continue;
                };
                let Ok(end_index) = line
                    .spans
                    .binary_search_by_key(&found.end(), |span| span.byte_end)
                else {
                    continue;
                };
                let start_span = &line.spans[start_index];
                let end_span = &line.spans[end_index];
                matches.push(TerminalTextMatch {
                    start: start_span.start,
                    end: end_span.end,
                    source_fingerprint: text_fingerprint(found.as_str()),
                    scan_cols: self.cols,
                    scan_screen: active_screen,
                });
            }
        }
        matches
    }

    fn contains_match(&self, text_match: TerminalTextMatch) -> bool {
        self.lines.iter().any(|line| {
            let Ok(start_index) = line
                .spans
                .binary_search_by_key(&text_match.start, |span| span.start)
            else {
                return false;
            };
            let Ok(end_index) = line
                .spans
                .binary_search_by_key(&text_match.end, |span| span.end)
            else {
                return false;
            };
            let start_span = &line.spans[start_index];
            let end_span = &line.spans[end_index];
            start_span.byte_start <= end_span.byte_end
                && text_fingerprint(&line.text[start_span.byte_start..end_span.byte_end])
                    == text_match.source_fingerprint
        })
    }

    fn word_motion(
        &self,
        row: u32,
        col: u16,
        motion: TerminalWordMotion,
    ) -> Option<TerminalTextPoint> {
        let current = self.atoms.iter().position(|atom| {
            atom.point
                .is_some_and(|point| point.row == row && col >= point.col && col <= atom.end_col)
        })?;
        match motion {
            TerminalWordMotion::NextStart => self.next_word_start(current),
            TerminalWordMotion::PreviousStart => self.previous_word_start(current),
            TerminalWordMotion::NextEnd => self.next_word_end(current),
        }
    }

    fn next_word_start(&self, current: usize) -> Option<TerminalTextPoint> {
        let current_class = self.atoms.get(current)?.class;
        let mut next = current.saturating_add(1);
        if current_class != TextClass::Whitespace {
            while self
                .atoms
                .get(next)
                .is_some_and(|atom| atom.class == current_class)
            {
                next += 1;
            }
        }
        while self
            .atoms
            .get(next)
            .is_some_and(|atom| atom.class == TextClass::Whitespace)
        {
            next += 1;
        }
        self.next_point(next)
    }

    fn previous_word_start(&self, current: usize) -> Option<TerminalTextPoint> {
        let mut previous = current.checked_sub(1)?;
        while self
            .atoms
            .get(previous)
            .is_some_and(|atom| atom.class == TextClass::Whitespace)
        {
            previous = previous.checked_sub(1)?;
        }
        let class = self.atoms.get(previous)?.class;
        while previous > 0
            && self
                .atoms
                .get(previous - 1)
                .is_some_and(|atom| atom.class == class)
        {
            previous -= 1;
        }
        self.previous_point(previous)
    }

    fn next_word_end(&self, current: usize) -> Option<TerminalTextPoint> {
        let mut next = current.saturating_add(1);
        while self
            .atoms
            .get(next)
            .is_some_and(|atom| atom.class == TextClass::Whitespace)
        {
            next += 1;
        }
        let class = self.atoms.get(next)?.class;
        while self
            .atoms
            .get(next + 1)
            .is_some_and(|atom| atom.class == class)
        {
            next += 1;
        }
        self.previous_point(next)
    }

    fn next_point(&self, mut index: usize) -> Option<TerminalTextPoint> {
        while let Some(atom) = self.atoms.get(index) {
            if let Some(point) = atom.point {
                return Some(point);
            }
            index += 1;
        }
        None
    }

    fn previous_point(&self, mut index: usize) -> Option<TerminalTextPoint> {
        loop {
            if let Some(point) = self.atoms.get(index)?.point {
                return Some(point);
            }
            index = index.checked_sub(1)?;
        }
    }

    fn point_is_final_atom(&self, point: TerminalTextPoint) -> bool {
        // Word motion targets are atom start points, so compare against the
        // final atom's start point. Comparing against `end_col` would never
        // match a wide glyph, whose end column is one past its start.
        self.atoms
            .iter()
            .rev()
            .find(|atom| atom.point.is_some())
            .is_some_and(|atom| atom.point == Some(point))
    }
}

fn terminal_cell_text(graphemes: &[u32]) -> String {
    if graphemes.is_empty()
        || graphemes.first().copied() == Some(crate::ghostty::KITTY_UNICODE_PLACEHOLDER)
    {
        return " ".to_string();
    }
    graphemes
        .iter()
        .map(|codepoint| char::from_u32(*codepoint).unwrap_or(char::REPLACEMENT_CHARACTER))
        .collect()
}

fn text_class(text: &str) -> TextClass {
    let Some(ch) = text.chars().next() else {
        return TextClass::Whitespace;
    };
    if ch.is_whitespace() {
        TextClass::Whitespace
    } else if ch.is_ascii() && COPY_MODE_WORD_SEPARATORS.contains(ch) {
        TextClass::Separator
    } else {
        TextClass::Word
    }
}

fn text_fingerprint(text: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    hasher.finish()
}

include!("terminal_io.rs");
include!("terminal_render.rs");
include!("terminal_style.rs");

#[cfg(test)]
mod grapheme_cluster_mode_tests {
    #[test]
    fn grapheme_cluster_mode_is_default_and_survives_full_reset() {
        let mut terminal = crate::ghostty::Terminal::new(80, 3, 100).unwrap();
        assert!(terminal
            .mode_get(crate::ghostty::MODE_GRAPHEME_CLUSTER)
            .unwrap());
        terminal.write(b"\x1bc");
        assert!(terminal
            .mode_get(crate::ghostty::MODE_GRAPHEME_CLUSTER)
            .unwrap());
    }
}

#[cfg(test)]
#[path = "terminal/tests.rs"]
mod tests;
