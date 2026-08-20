impl Terminal {
    pub fn new(cols: u16, rows: u16, max_scrollback: usize) -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        let options = ffi::GhosttyTerminalOptions {
            cols,
            rows,
            max_scrollback,
        };
        // SAFETY: valid out pointer and options, null allocator means default allocator.
        unsafe {
            ffi::ghostty_terminal_new(ptr::null(), &mut raw, options).into_result()?;
        }

        let mut terminal = Self {
            raw,
            callback_state: Box::new(TerminalCallbackState {
                size_report: ffi::GhosttySizeReportSize {
                    rows,
                    columns: cols,
                    ..Default::default()
                },
                ..Default::default()
            }),
            kitty_fingerprints: Mutex::new(HashMap::new()),
            kitty_empty_generation: Cell::new(None),
        };
        let userdata = (&mut *terminal.callback_state as *mut TerminalCallbackState).cast();
        let glyph_protocol = false;
        unsafe {
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_USERDATA,
                userdata,
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_SIZE,
                (size_trampoline as *const ()).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_PWD_CHANGED,
                (pwd_changed_trampoline as *const ()).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE,
                (clipboard_write_trampoline as *const ()).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_SCHEME,
                (color_scheme_trampoline as *const ()).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                terminal.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_GLYPH_PROTOCOL,
                (&glyph_protocol as *const bool).cast(),
            )
            .into_result()?;
        }
        Ok(terminal)
    }

    pub fn write(&mut self, bytes: &[u8]) {
        // SAFETY: self.raw is a live terminal handle for self's lifetime.
        unsafe {
            ffi::ghostty_terminal_vt_write(self.raw, bytes.as_ptr(), bytes.len());
        }
    }

    pub fn set_default_palette(&mut self, palette: &[RgbColor; 256]) -> Result<(), Error> {
        let palette = palette.map(|color| ffi::GhosttyColorRgb {
            r: color.r,
            g: color.g,
            b: color.b,
        });
        unsafe {
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_PALETTE,
                palette.as_ptr().cast(),
            )
            .into_result()
        }
    }

    pub fn resize(
        &mut self,
        cols: u16,
        rows: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    ) -> Result<(), Error> {
        let size_report = ffi::GhosttySizeReportSize {
            rows,
            columns: cols,
            cell_width: cell_width_px,
            cell_height: cell_height_px,
        };
        // SAFETY: self.raw is valid and sizes are plain values.
        unsafe {
            ffi::ghostty_terminal_resize(
                self.raw,
                cols,
                rows,
                cell_width_px.max(1),
                cell_height_px.max(1),
            )
            .into_result()?;
        }
        self.callback_state.size_report = size_report;
        Ok(())
    }

    pub fn enable_kitty_graphics(&mut self) -> Result<(), Error> {
        install_png_decoder_once();
        let storage_limit = KITTY_IMAGE_STORAGE_LIMIT_BYTES;
        let enable_medium = true;
        unsafe {
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT,
                (&storage_limit as *const u64).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_FILE,
                (&enable_medium as *const bool).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_TEMP_FILE,
                (&enable_medium as *const bool).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_SHARED_MEM,
                (&enable_medium as *const bool).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_APC_MAX_BYTES,
                (&APC_MAX_BYTES as *const usize).cast(),
            )
            .into_result()?;
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_APC_MAX_BYTES_KITTY,
                (&APC_MAX_BYTES_KITTY as *const usize).cast(),
            )
            .into_result()?;
        }
        Ok(())
    }

    pub fn set_write_pty_callback<F>(&mut self, callback: F) -> Result<(), Error>
    where
        F: FnMut(&[u8]) + Send + 'static,
    {
        unsafe {
            ffi::ghostty_terminal_set(
                self.raw,
                ffi::GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_WRITE_PTY,
                (write_pty_trampoline as *const ()).cast(),
            )
            .into_result()?;
        }
        self.callback_state.write_pty = Some(Box::new(callback));
        Ok(())
    }

    pub fn set_color_scheme(&mut self, color_scheme: Option<ColorScheme>) -> Option<ColorScheme> {
        mem::replace(&mut self.callback_state.color_scheme, color_scheme)
    }

    pub fn take_pwd_changes(&mut self) -> Vec<Vec<u8>> {
        mem::take(&mut self.callback_state.pwd_changes)
    }

    pub fn take_clipboard_writes(&mut self) -> Vec<Vec<u8>> {
        mem::take(&mut self.callback_state.clipboard_writes)
    }

    pub fn mode_get(&self, mode: u16) -> Result<bool, Error> {
        let mut out = false;
        unsafe { ffi::ghostty_terminal_mode_get(self.raw, mode, &mut out).into_result()? };
        Ok(out)
    }

    pub fn mode_set(&mut self, mode: u16, value: bool) -> Result<(), Error> {
        unsafe { ffi::ghostty_terminal_mode_set(self.raw, mode, value).into_result() }
    }

    pub fn kitty_keyboard_flags(&self) -> Result<u8, Error> {
        let mut out = 0u8;
        unsafe {
            ffi::ghostty_terminal_get(
                self.raw,
                ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_KEYBOARD_FLAGS,
                (&mut out as *mut u8).cast(),
            )
            .into_result()?;
        }
        Ok(out)
    }

    pub fn mouse_tracking_enabled(&self) -> Result<bool, Error> {
        self.get_bool(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING)
    }

    pub fn active_screen(&self) -> Result<ActiveScreen, Error> {
        let mut out = ffi::GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_PRIMARY;
        unsafe {
            ffi::ghostty_terminal_get(
                self.raw,
                ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN,
                (&mut out as *mut ffi::GhosttyTerminalScreen).cast(),
            )
            .into_result()?;
        }
        Ok(match out {
            ffi::GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_PRIMARY => ActiveScreen::Primary,
            ffi::GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_ALTERNATE => ActiveScreen::Alternate,
            _ => ActiveScreen::Primary,
        })
    }

    pub fn total_rows(&self) -> Result<usize, Error> {
        self.get_usize(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_TOTAL_ROWS)
    }

    pub fn scrollback_rows(&self) -> Result<usize, Error> {
        self.get_usize(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBACK_ROWS)
    }

    pub fn scrollbar(&self) -> Result<TerminalScrollbar, Error> {
        let mut out = ffi::GhosttyTerminalScrollbar::default();
        unsafe {
            ffi::ghostty_terminal_get(
                self.raw,
                ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBAR,
                (&mut out as *mut ffi::GhosttyTerminalScrollbar).cast(),
            )
            .into_result()?;
        }
        Ok(TerminalScrollbar {
            total: out.total as usize,
            offset: out.offset as usize,
            len: out.len as usize,
        })
    }

    pub fn screen_cell(&self, x: u16, y: u32) -> Result<(CellWide, Vec<u32>), Error> {
        let grid_ref = self.grid_ref(ghostty_screen_point(x, y))?;
        let wide = grid_ref_wide(&grid_ref)?;
        let graphemes = grid_ref_graphemes(&grid_ref)?;
        Ok((wide, graphemes))
    }

    pub(crate) fn screen_text_rows(&self) -> Result<Vec<ScreenTextRow>, Error> {
        self.screen_text_rows_range(0, usize::MAX)
    }

    pub(crate) fn screen_text_rows_range(
        &self,
        start_row: usize,
        end_row_exclusive: usize,
    ) -> Result<Vec<ScreenTextRow>, Error> {
        let total_rows = self.total_rows()?;
        let start_row = start_row.min(total_rows);
        let end_row_exclusive = end_row_exclusive.min(total_rows).max(start_row);
        let cols = self.cols()?;
        let mut rows = Vec::with_capacity(end_row_exclusive.saturating_sub(start_row));
        for y in start_row..end_row_exclusive {
            let Some(y) = u32::try_from(y).ok() else {
                break;
            };
            let mut grid_ref = self.grid_ref(ghostty_screen_point(0, y))?;
            let (soft_wrapped, wrap_continuation) = grid_ref_wrap_state(&grid_ref)?;
            let mut cells = Vec::with_capacity(usize::from(cols));
            for x in 0..cols {
                grid_ref.x = x;
                cells.push(ScreenTextCell {
                    wide: grid_ref_wide(&grid_ref)?,
                    graphemes: grid_ref_graphemes(&grid_ref)?,
                });
            }
            rows.push(ScreenTextRow {
                cells,
                soft_wrapped,
                wrap_continuation,
            });
        }
        Ok(rows)
    }

    fn viewport_graphemes_and_style(&self, x: u16, y: u32) -> Result<(Vec<u32>, CellStyle), Error> {
        let grid_ref = self.grid_ref(ghostty_viewport_point(x, y))?;
        let graphemes = grid_ref_graphemes(&grid_ref)?;
        let mut style = ffi::GhosttyStyle {
            size: mem::size_of::<ffi::GhosttyStyle>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_grid_ref_style(&grid_ref, &mut style).into_result()?;
        }
        Ok((graphemes, style.into()))
    }

    pub fn viewport_hyperlink_uri(&self, x: u16, y: u32) -> Result<Option<String>, Error> {
        let grid_ref = self.grid_ref(ghostty_viewport_point(x, y))?;
        grid_ref_hyperlink_uri(&grid_ref)
    }

    fn grid_ref(&self, point: ffi::GhosttyPoint) -> Result<ffi::GhosttyGridRef, Error> {
        let mut grid_ref = ffi::GhosttyGridRef {
            size: mem::size_of::<ffi::GhosttyGridRef>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_terminal_grid_ref(self.raw, point, &mut grid_ref).into_result()?;
        }
        Ok(grid_ref)
    }

    pub fn read_text_viewport(
        &self,
        start: (u16, u32),
        end: (u16, u32),
        rectangle: bool,
    ) -> Result<String, Error> {
        self.read_formatted_selection(
            ghostty_viewport_point(start.0, start.1),
            ghostty_viewport_point(end.0, end.1),
            rectangle,
            FormatterFormat::Plain,
            true,
            true,
        )
    }

    pub fn read_ansi_viewport(
        &self,
        start: (u16, u32),
        end: (u16, u32),
        rectangle: bool,
    ) -> Result<String, Error> {
        self.read_formatted_selection(
            ghostty_viewport_point(start.0, start.1),
            ghostty_viewport_point(end.0, end.1),
            rectangle,
            FormatterFormat::Vt,
            false,
            true,
        )
    }

    pub fn read_text_screen(
        &self,
        start: (u16, u32),
        end: (u16, u32),
        rectangle: bool,
    ) -> Result<String, Error> {
        self.read_formatted_selection(
            ghostty_screen_point(start.0, start.1),
            ghostty_screen_point(end.0, end.1),
            rectangle,
            FormatterFormat::Plain,
            true,
            true,
        )
    }

    pub fn read_ansi_screen(
        &self,
        start: (u16, u32),
        end: (u16, u32),
        rectangle: bool,
        unwrap: bool,
    ) -> Result<String, Error> {
        self.read_formatted_selection(
            ghostty_screen_point(start.0, start.1),
            ghostty_screen_point(end.0, end.1),
            rectangle,
            FormatterFormat::Vt,
            unwrap,
            true,
        )
    }

    pub fn keyboard_state_ansi(&self) -> Result<String, Error> {
        self.format_keyboard_state_ansi(false)
    }

    pub fn kitty_keyboard_state_ansi(&self) -> Result<String, Error> {
        self.format_keyboard_state_ansi(true)
    }

    fn format_keyboard_state_ansi(&self, kitty_keyboard: bool) -> Result<String, Error> {
        let mut formatter: ffi::GhosttyFormatter = ptr::null_mut();
        let options = ffi::GhosttyFormatterTerminalOptions {
            size: mem::size_of::<ffi::GhosttyFormatterTerminalOptions>(),
            emit: FormatterFormat::Vt.as_raw(),
            unwrap: false,
            trim: false,
            extra: ffi::GhosttyFormatterTerminalExtra {
                size: mem::size_of::<ffi::GhosttyFormatterTerminalExtra>(),
                keyboard: true,
                screen: ffi::GhosttyFormatterScreenExtra {
                    size: mem::size_of::<ffi::GhosttyFormatterScreenExtra>(),
                    kitty_keyboard,
                    ..Default::default()
                },
                ..Default::default()
            },
            selection: ptr::null(),
        };
        unsafe {
            ffi::ghostty_formatter_terminal_new(ptr::null(), &mut formatter, self.raw, options)
                .into_result()?;
        }

        let mut out_ptr = ptr::null_mut();
        let mut out_len = 0usize;
        let result = unsafe {
            ffi::ghostty_formatter_format_alloc(formatter, ptr::null(), &mut out_ptr, &mut out_len)
        };
        unsafe {
            ffi::ghostty_formatter_free(formatter);
        }
        result.into_result()?;

        let text = if out_len == 0 {
            String::new()
        } else {
            let bytes = unsafe { slice::from_raw_parts(out_ptr.cast_const(), out_len) };
            String::from_utf8_lossy(bytes).into_owned()
        };

        if !out_ptr.is_null() {
            unsafe {
                ffi::ghostty_free(ptr::null(), out_ptr, out_len);
            }
        }

        Ok(text)
    }

    fn read_formatted_selection(
        &self,
        start: ffi::GhosttyPoint,
        end: ffi::GhosttyPoint,
        rectangle: bool,
        format: FormatterFormat,
        unwrap: bool,
        trim: bool,
    ) -> Result<String, Error> {
        let mut start_ref = ffi::GhosttyGridRef {
            size: mem::size_of::<ffi::GhosttyGridRef>(),
            ..Default::default()
        };
        let mut end_ref = ffi::GhosttyGridRef {
            size: mem::size_of::<ffi::GhosttyGridRef>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_terminal_grid_ref(self.raw, start, &mut start_ref).into_result()?;
            ffi::ghostty_terminal_grid_ref(self.raw, end, &mut end_ref).into_result()?;
        }

        let selection = ffi::GhosttySelection {
            size: mem::size_of::<ffi::GhosttySelection>(),
            start: start_ref,
            end: end_ref,
            rectangle,
        };
        let mut formatter: ffi::GhosttyFormatter = ptr::null_mut();
        let options = ffi::GhosttyFormatterTerminalOptions {
            size: mem::size_of::<ffi::GhosttyFormatterTerminalOptions>(),
            emit: format.as_raw(),
            unwrap,
            trim,
            extra: ffi::GhosttyFormatterTerminalExtra {
                size: mem::size_of::<ffi::GhosttyFormatterTerminalExtra>(),
                screen: ffi::GhosttyFormatterScreenExtra {
                    size: mem::size_of::<ffi::GhosttyFormatterScreenExtra>(),
                    ..Default::default()
                },
                ..Default::default()
            },
            selection: &selection,
        };
        unsafe {
            ffi::ghostty_formatter_terminal_new(ptr::null(), &mut formatter, self.raw, options)
                .into_result()?;
        }

        let mut out_ptr = ptr::null_mut();
        let mut out_len = 0usize;
        let result = unsafe {
            ffi::ghostty_formatter_format_alloc(formatter, ptr::null(), &mut out_ptr, &mut out_len)
        };
        unsafe {
            ffi::ghostty_formatter_free(formatter);
        }
        result.into_result()?;

        let text = if out_len == 0 {
            String::new()
        } else {
            let bytes = unsafe { slice::from_raw_parts(out_ptr.cast_const(), out_len) };
            String::from_utf8_lossy(bytes).into_owned()
        };

        if !out_ptr.is_null() {
            unsafe {
                ffi::ghostty_free(ptr::null(), out_ptr, out_len);
            }
        }

        Ok(text)
    }

    pub fn scroll_viewport_bottom(&mut self) {
        let viewport = ffi::GhosttyTerminalScrollViewport {
            tag: ffi::GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_BOTTOM,
            value: ffi::GhosttyTerminalScrollViewportValue::default(),
        };
        // SAFETY: self.raw is valid and viewport value matches the tag.
        unsafe {
            ffi::ghostty_terminal_scroll_viewport(self.raw, viewport);
        }
    }

    pub fn scroll_viewport_delta(&mut self, delta: isize) {
        let viewport = ffi::GhosttyTerminalScrollViewport {
            tag: ffi::GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_DELTA,
            value: ffi::GhosttyTerminalScrollViewportValue { delta },
        };
        // SAFETY: self.raw is valid and viewport value matches the tag.
        unsafe {
            ffi::ghostty_terminal_scroll_viewport(self.raw, viewport);
        }
    }

    pub fn scroll_viewport_row(&mut self, row: usize) {
        let viewport = ffi::GhosttyTerminalScrollViewport {
            tag: ffi::GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_ROW,
            value: ffi::GhosttyTerminalScrollViewportValue { row },
        };
        // SAFETY: self.raw is valid and viewport value matches the tag.
        unsafe {
            ffi::ghostty_terminal_scroll_viewport(self.raw, viewport);
        }
    }

    pub fn cols(&self) -> Result<u16, Error> {
        self.get_u16(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLS)
    }

    pub fn rows(&self) -> Result<u16, Error> {
        self.get_u16(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_ROWS)
    }

    pub fn effective_foreground_color(&self) -> Result<Option<RgbColor>, Error> {
        self.get_optional_rgb_color(TERMINAL_DATA_COLOR_FOREGROUND)
    }

    pub fn effective_cursor_color(&self) -> Result<Option<RgbColor>, Error> {
        self.get_optional_rgb_color(TERMINAL_DATA_COLOR_CURSOR)
    }

    fn width_px(&self) -> Result<u32, Error> {
        self.get_u32(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_WIDTH_PX)
    }

    fn height_px(&self) -> Result<u32, Error> {
        self.get_u32(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_HEIGHT_PX)
    }

    fn get_u16(&self, data: ffi::GhosttyTerminalData) -> Result<u16, Error> {
        let mut out = 0u16;
        // SAFETY: out points to a u16 matching the requested terminal data type.
        unsafe {
            ffi::ghostty_terminal_get(self.raw, data, (&mut out as *mut u16).cast())
                .into_result()?;
        }
        Ok(out)
    }

    fn get_u32(&self, data: ffi::GhosttyTerminalData) -> Result<u32, Error> {
        let mut out = 0u32;
        // SAFETY: out points to a u32 matching the requested terminal data type.
        unsafe {
            ffi::ghostty_terminal_get(self.raw, data, (&mut out as *mut u32).cast())
                .into_result()?;
        }
        Ok(out)
    }

    fn get_usize(&self, data: ffi::GhosttyTerminalData) -> Result<usize, Error> {
        let mut out = 0usize;
        unsafe {
            ffi::ghostty_terminal_get(self.raw, data, (&mut out as *mut usize).cast())
                .into_result()?;
        }
        Ok(out)
    }

    fn get_bool(&self, data: ffi::GhosttyTerminalData) -> Result<bool, Error> {
        let mut out = false;
        unsafe {
            ffi::ghostty_terminal_get(self.raw, data, (&mut out as *mut bool).cast())
                .into_result()?;
        }
        Ok(out)
    }

    fn get_optional_rgb_color(
        &self,
        data: ffi::GhosttyTerminalData,
    ) -> Result<Option<RgbColor>, Error> {
        let mut out = ffi::GhosttyColorRgb::default();
        let result = unsafe {
            ffi::ghostty_terminal_get(
                self.raw,
                data,
                (&mut out as *mut ffi::GhosttyColorRgb).cast(),
            )
        };
        match result {
            ffi::GhosttyResult_GHOSTTY_SUCCESS => Ok(Some(out.into())),
            ffi::GhosttyResult_GHOSTTY_NO_VALUE => Ok(None),
            other => Err(Error(other)),
        }
    }

    fn kitty_graphics(&self) -> Result<ffi::GhosttyKittyGraphics, Error> {
        let mut graphics: ffi::GhosttyKittyGraphics = ptr::null_mut();
        unsafe {
            ffi::ghostty_terminal_get(
                self.raw,
                ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,
                (&mut graphics as *mut ffi::GhosttyKittyGraphics).cast(),
            )
            .into_result()?;
        }
        Ok(graphics)
    }

    pub fn kitty_graphics_generation(&self) -> Result<u64, Error> {
        let graphics = self.kitty_graphics()?;
        if graphics.is_null() {
            return Ok(0);
        }
        kitty_graphics_u64(
            graphics,
            ffi::GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION,
        )
    }

    pub fn kitty_image_placements(&self) -> Result<Vec<KittyImagePlacement>, Error> {
        self.kitty_image_placements_with_data_filter(|_| true)
    }

}
