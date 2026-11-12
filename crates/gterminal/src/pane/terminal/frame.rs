use super::*;

impl PaneTerminal {
    pub(crate) fn frame_data(&self, cols: u16, rows: u16) -> crate::protocol::FrameData {
        #[cfg(test)]
        self.note_work_started();
        let cols = cols.max(1);
        let rows = rows.max(1);
        let area = ratatui::layout::Rect::new(0, 0, cols, rows);
        let mut buffer = ratatui::buffer::Buffer::filled(area, ratatui::buffer::Cell::new(" "));
        let cursor = self.render_to_buffer(&mut buffer, area);
        let protocol_cursor = cursor
            .filter(|cursor| cursor.visible && cursor.x < cols && cursor.y < rows)
            .map(|cursor| crate::protocol::CursorState {
                x: cursor.x,
                y: cursor.y,
                visible: cursor.visible,
                shape: cursor.shape,
            });
        let hyperlinks = self.visible_hyperlinks(area);
        let mut frame = crate::protocol::FrameData::from_ratatui_buffer_with_hyperlinks(
            &buffer,
            protocol_cursor,
            &hyperlinks,
        );
        if let Ok(core) = self.ghostty.core.lock() {
            const MODE_MOUSE_X10: u16 = 9;
            const MODE_MOUSE_PRESS_RELEASE: u16 = 1000;
            const MODE_MOUSE_BUTTON_MOTION: u16 = 1002;
            const MODE_MOUSE_ANY_MOTION: u16 = 1003;
            frame.modes = crate::protocol::PaneModes {
                mouse_standard: core
                    .terminal
                    .mode_get(MODE_MOUSE_PRESS_RELEASE)
                    .unwrap_or(false),
                mouse_button: core
                    .terminal
                    .mode_get(MODE_MOUSE_BUTTON_MOTION)
                    .unwrap_or(false),
                mouse_all: core
                    .terminal
                    .mode_get(MODE_MOUSE_ANY_MOTION)
                    .unwrap_or(false),
                mouse_any: core.terminal.mode_get(MODE_MOUSE_X10).unwrap_or(false),
                mouse_sgr: core
                    .terminal
                    .mode_get(crate::ghostty::MODE_MOUSE_SGR)
                    .unwrap_or(false),
                alternate_on: core.terminal.active_screen().ok()
                    == Some(crate::ghostty::ActiveScreen::Alternate),
                kitty_keyboard_flags: core
                    .terminal
                    .kitty_keyboard_flags()
                    .map(u16::from)
                    .unwrap_or(0),
                ..Default::default()
            };
        }
        frame
    }

    pub(crate) fn frame_for_view(
        &self,
        cols: u16,
        rows: u16,
        scroll: u32,
    ) -> (u64, crate::protocol::FrameData) {
        #[cfg(test)]
        self.note_work_started();
        let _view = self.frame_view.lock().unwrap_or_else(|e| e.into_inner());
        let _mutation = self
            .history_mutation
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        self.set_scroll_offset_from_bottom(scroll as usize);
        let frame = self.frame_data(cols, rows);
        self.scroll_reset();
        (self.content_generation(), frame)
    }
}
