//! Live terminal runtime handle.

use std::path::PathBuf;

use bytes::Bytes;
use tokio::sync::mpsc;

use crate::pane::{PaneRuntime, PaneShellConfig, TerminalDirtyPatchOutcome};
use crate::protocol::FrameData;
use crate::terminal_theme::{HostAppearance, TerminalTheme};

pub struct TerminalRuntime(PaneRuntime);

impl TerminalRuntime {
    pub fn spawn(
        rows: u16,
        cols: u16,
        cwd: PathBuf,
        scrollback_limit_bytes: usize,
        host_terminal_theme: TerminalTheme,
        host_terminal_appearance: Option<HostAppearance>,
        shell_config: PaneShellConfig<'_>,
    ) -> std::io::Result<Self> {
        PaneRuntime::spawn(
            rows,
            cols,
            cwd,
            scrollback_limit_bytes,
            host_terminal_theme,
            host_terminal_appearance,
            shell_config,
        )
        .map(Self)
    }

    pub fn shutdown(self) {
        self.0.shutdown();
    }

    pub fn kill(self) {
        self.0.kill();
    }

    pub fn resize(&self, rows: u16, cols: u16, cell_width_px: u32, cell_height_px: u32) {
        self.0.resize(rows, cols, cell_width_px, cell_height_px);
    }

    pub fn scroll_up(&self, lines: usize) {
        self.0.scroll_up(lines);
    }

    pub fn scroll_down(&self, lines: usize) {
        self.0.scroll_down(lines);
    }

    pub fn scroll_reset(&self) {
        self.0.scroll_reset();
    }

    pub fn snapshot_history(&self) -> Option<String> {
        self.0.snapshot_history()
    }

    pub fn visible_text(&self) -> String {
        self.0.visible_text()
    }

    pub fn frame_data(&self, cols: u16, rows: u16) -> FrameData {
        self.0.frame_data(cols, rows)
    }

    pub fn dirty_patch(&self) -> TerminalDirtyPatchOutcome {
        self.0.dirty_patch()
    }

    pub fn osc_title(&self) -> String {
        self.0.osc_title()
    }

    pub fn osc_progress(&self) -> String {
        self.0.osc_progress()
    }

    pub fn encode_terminal_key(&self, key: crate::input::TerminalKey) -> Vec<u8> {
        self.0.encode_terminal_key(key)
    }

    pub fn encode_mouse_button(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        self.0.encode_mouse_button(kind, column, row, modifiers)
    }

    pub async fn send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::SendError<Bytes>> {
        self.0.send_bytes(bytes).await
    }

    pub fn try_send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::TrySendError<Bytes>> {
        self.0.try_send_bytes(bytes)
    }

    pub fn child_pid(&self) -> Option<u32> {
        self.0.child_pid()
    }

    pub fn render(
        &self,
        frame: &mut ratatui::Frame,
        area: ratatui::layout::Rect,
        show_cursor: bool,
    ) {
        self.0.render(frame, area, show_cursor);
    }

    #[cfg(unix)]
    pub fn set_handoff_reader_paused(&self, paused: bool) {
        self.0.set_handoff_reader_paused(paused);
    }

    #[cfg(unix)]
    pub fn assume_handoff_ownership(&mut self) {
        self.0.assume_handoff_ownership();
    }

    #[cfg(unix)]
    pub fn nudge_child_redraw_after_handoff(&self) {
        self.0.nudge_child_redraw_after_handoff();
    }
}
