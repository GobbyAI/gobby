impl PaneRuntime {
    #[cfg(unix)]
    pub fn duplicate_handoff_fd(&self) -> std::io::Result<std::os::fd::RawFd> {
        self.io.duplicate_handoff_fd()
    }

    #[cfg(unix)]
    pub fn preserve_for_handoff(mut self) {
        if let Err(err) = self.io.release_after_commit() {
            warn!(
                pane = self.pane_id.raw(),
                err = %err,
                "failed to release PTY actor after handoff commit"
            );
        }
        self.preserve_processes_on_drop = true;
    }

    #[cfg(unix)]
    pub fn assume_handoff_ownership(&mut self) {
        self.preserve_processes_on_drop = false;
    }

    #[cfg(unix)]
    pub fn set_handoff_reader_paused(&self, paused: bool) {
        if let Err(err) = self.io.set_handoff_paused(paused) {
            warn!(
                pane = self.pane_id.raw(),
                err = %err,
                paused,
                "failed to update PTY actor handoff pause state"
            );
        }
    }

    #[cfg(unix)]
    pub fn pause_handoff_reader(&self, timeout: std::time::Duration) -> std::io::Result<()> {
        self.io.begin_handoff(timeout)
    }

    pub fn apply_host_terminal_theme(&self, theme: crate::terminal_theme::TerminalTheme) {
        self.terminal.apply_host_terminal_theme(theme);
    }

    pub fn apply_host_terminal_appearance(
        &self,
        appearance: Option<crate::terminal_theme::HostAppearance>,
    ) {
        self.io
            .write_terminal_response(|| self.terminal.apply_host_terminal_appearance(appearance));
    }

    pub(crate) fn current_size(&self) -> (u16, u16) {
        let (rows, cols, _, _) = self.current_size.get();
        (rows, cols)
    }

    pub fn resize(&self, rows: u16, cols: u16, cell_width_px: u32, cell_height_px: u32) {
        let rows = rows.max(2);
        let cols = cols.max(4);
        let size = (rows, cols, cell_width_px, cell_height_px);
        if self.current_size.get() == size {
            return;
        }
        self.current_size.set(size);
        let terminal_responses = self
            .terminal
            .resize(rows, cols, cell_width_px, cell_height_px);
        self.io.resize(
            rows,
            cols,
            cell_width_px,
            cell_height_px,
            terminal_responses,
        );
        self.render_notify.notify_one();
    }

    #[cfg(unix)]
    pub fn nudge_child_redraw_after_handoff(&self) {
        let (rows, cols, cell_width_px, cell_height_px) = self.current_size.get();
        self.io
            .nudge_child_redraw_after_handoff(rows, cols, cell_width_px, cell_height_px);
    }

    pub fn scroll_up(&self, lines: usize) {
        self.terminal.scroll_up(lines);
    }

    pub fn scroll_down(&self, lines: usize) {
        self.terminal.scroll_down(lines);
    }

    pub fn scroll_reset(&self) {
        self.terminal.scroll_reset();
    }

    pub fn set_scroll_offset_from_bottom(&self, lines: usize) {
        self.terminal.set_scroll_offset_from_bottom(lines);
    }

    pub fn scroll_metrics(&self) -> Option<super::ScrollMetrics> {
        self.terminal.scroll_metrics()
    }

    pub fn input_state(&self) -> Option<super::InputState> {
        self.terminal.input_state()
    }

    pub fn alternate_screen_active(&self) -> bool {
        self.terminal.alternate_screen_active()
    }

    pub fn cursor_state(
        &self,
        area: ratatui::layout::Rect,
        show_cursor: bool,
    ) -> Option<super::TerminalCursorState> {
        if !show_cursor {
            return None;
        }
        let cursor = self.terminal.cursor_state()?;
        if cursor.x >= area.width || cursor.y >= area.height {
            return None;
        }
        Some(super::TerminalCursorState {
            x: area.x + cursor.x,
            y: area.y + cursor.y,
            visible: cursor.visible,
            shape: cursor.shape,
        })
    }

    pub fn synchronized_output_active(&self) -> bool {
        self.terminal.synchronized_output_active()
    }

    pub fn visible_text(&self) -> String {
        self.terminal.visible_text()
    }

    pub fn visible_ansi(&self) -> String {
        self.terminal.visible_ansi()
    }

    pub fn terminal_title(&self) -> Option<String> {
        self.terminal.terminal_title()
    }

    pub fn osc_title(&self) -> String {
        self.terminal.osc_title()
    }

    pub fn osc_progress(&self) -> String {
        self.terminal.osc_progress()
    }

    pub fn snapshot_history(&self) -> Option<String> {
        let ansi = self.terminal.recent_unwrapped_ansi(usize::MAX);
        (!ansi.trim().is_empty()).then_some(ansi)
    }

    pub fn extract_selection(&self, selection: &crate::selection::Selection) -> Option<String> {
        self.terminal.extract_selection(selection)
    }

    pub fn render(&self, frame: &mut ratatui::Frame, area: ratatui::layout::Rect, show_cursor: bool) {
        self.terminal.render(frame, area, show_cursor);
    }

    pub fn frame_data(&self, cols: u16, rows: u16) -> crate::protocol::FrameData {
        let cols = cols.max(1);
        let rows = rows.max(1);
        let area = ratatui::layout::Rect::new(0, 0, cols, rows);
        let mut buffer = ratatui::buffer::Buffer::filled(area, ratatui::buffer::Cell::new(" "));
        let cursor = self.terminal.render_to_buffer(&mut buffer, area);
        let protocol_cursor = cursor
            .filter(|cursor| cursor.visible && cursor.x < cols && cursor.y < rows)
            .map(|cursor| crate::protocol::CursorState {
                x: cursor.x,
                y: cursor.y,
                visible: cursor.visible,
                shape: cursor.shape,
            });
        let hyperlinks = self.terminal.visible_hyperlinks(area);
        crate::protocol::FrameData::from_ratatui_buffer_with_hyperlinks(
            &buffer,
            protocol_cursor,
            &hyperlinks,
        )
    }

    pub fn dirty_patch(&self) -> super::TerminalDirtyPatchOutcome {
        let (rows, cols, _, _) = self.current_size.get();
        self.terminal
            .collect_dirty_patch(cols.max(1), rows.max(1))
    }

    pub fn collect_dirty_patch(
        &self,
        area_width: u16,
        area_height: u16,
    ) -> super::TerminalDirtyPatchOutcome {
        self.terminal.collect_dirty_patch(area_width, area_height)
    }

    pub fn visible_hyperlinks(
        &self,
        area: ratatui::layout::Rect,
    ) -> Vec<((u16, u16), String, String)> {
        self.terminal.visible_hyperlinks(area)
    }

    pub fn keyboard_protocol(&self) -> crate::input::KeyboardProtocol {
        let fallback = crate::input::KeyboardProtocol::from_kitty_flags(
            self.kitty_keyboard_flags.load(Ordering::Relaxed),
        );
        self.terminal.keyboard_protocol(fallback)
    }

    pub fn encode_terminal_key(&self, key: crate::input::TerminalKey) -> Vec<u8> {
        self.terminal
            .encode_terminal_key(key, self.keyboard_protocol())
    }

    pub async fn send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::SendError<Bytes>> {
        self.io.send_bytes(bytes).await
    }

    pub fn try_send_bytes(&self, bytes: Bytes) -> Result<(), mpsc::error::TrySendError<Bytes>> {
        self.io.try_send_bytes(bytes)
    }

    pub fn send_bytes_after(&self, bytes: Bytes, delay: std::time::Duration) {
        self.io.send_bytes_after(bytes, delay);
    }

    pub async fn send_paste(&self, text: String) -> Result<(), mpsc::error::SendError<Bytes>> {
        self.send_bytes(self.paste_payload(text)).await
    }

    pub fn try_send_paste(&self, text: String) -> Result<(), mpsc::error::TrySendError<Bytes>> {
        self.try_send_bytes(self.paste_payload(text))
    }

    fn paste_payload(&self, text: String) -> Bytes {
        let bracketed = self
            .input_state()
            .map(|state| state.bracketed_paste)
            .unwrap_or(false);
        let payload = if bracketed {
            format!("\x1b[200~{text}\x1b[201~")
        } else {
            text
        };
        Bytes::from(payload)
    }

    pub fn try_send_focus_event(&self, event: crate::ghostty::FocusEvent) -> bool {
        if !self
            .input_state()
            .map(|state| state.focus_reporting)
            .unwrap_or(false)
        {
            return false;
        }

        let Ok(bytes) = crate::ghostty::encode_focus(event) else {
            return false;
        };
        if let Err(err) = self.try_send_bytes(Bytes::from(bytes)) {
            warn!(err = %err, ?event, "failed to forward pane focus event");
        }
        true
    }

    pub fn wheel_routing(&self) -> Option<super::WheelRouting> {
        self.terminal.wheel_routing()
    }

    pub fn encode_mouse_button(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        if !self.input_state()?.mouse_protocol_mode.reporting_enabled() {
            return None;
        }
        self.terminal
            .encode_mouse_button(kind, column, row, modifiers)
    }

    pub fn encode_mouse_motion(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        self.terminal
            .encode_mouse_motion(kind, column, row, modifiers)
    }

    pub fn encode_mouse_wheel(
        &self,
        kind: crossterm::event::MouseEventKind,
        column: u16,
        row: u16,
        modifiers: crossterm::event::KeyModifiers,
    ) -> Option<Vec<u8>> {
        if self.wheel_routing()? != super::WheelRouting::MouseReport {
            return None;
        }
        self.terminal
            .encode_mouse_wheel(kind, column, row, modifiers)
    }

    pub fn encode_alternate_scroll(
        &self,
        kind: crossterm::event::MouseEventKind,
    ) -> Option<Vec<u8>> {
        self.input_state()?;
        if self.wheel_routing()? != super::WheelRouting::AlternateScroll {
            return None;
        }
        let key = match kind {
            crossterm::event::MouseEventKind::ScrollUp => crossterm::event::KeyCode::Up,
            crossterm::event::MouseEventKind::ScrollDown => crossterm::event::KeyCode::Down,
            _ => return None,
        };
        Some(self.encode_terminal_key(crate::input::TerminalKey::new(
            key,
            crossterm::event::KeyModifiers::empty(),
        )))
    }

    pub fn cwd(&self) -> Option<std::path::PathBuf> {
        if let Some(cwd) = self
            .reported_cwd
            .lock()
            .ok()
            .and_then(|reported_cwd| reported_cwd.clone())
        {
            return Some(cwd);
        }

        let pid = self.child_pid.load(Ordering::Relaxed);
        crate::platform::process_cwd(pid)
    }

    pub fn child_pid(&self) -> Option<u32> {
        let pid = self.child_pid.load(Ordering::Acquire);
        (pid > 0).then_some(pid)
    }

    pub fn follow_cwd(&self) -> Option<std::path::PathBuf> {
        #[cfg(unix)]
        {
            let leader_cwd = self
                .io
                .foreground_process_group_id()
                .and_then(crate::platform::process_cwd);
            leader_cwd.or_else(|| self.cwd())
        }

        #[cfg(not(unix))]
        {
            self.cwd()
        }
    }

    pub fn render_notify(&self) -> Arc<Notify> {
        self.render_notify.clone()
    }
}
