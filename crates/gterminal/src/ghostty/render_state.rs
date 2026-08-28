pub struct RenderState {
    raw: ffi::GhosttyRenderState,
}

impl RenderState {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        // SAFETY: valid out pointer and null allocator use default allocator.
        unsafe {
            ffi::ghostty_render_state_new(ptr::null(), &mut raw).into_result()?;
        }
        Ok(Self { raw })
    }

    pub fn update(&mut self, terminal: &Terminal) -> Result<(), Error> {
        // SAFETY: both handles are valid for the duration of the call.
        unsafe { ffi::ghostty_render_state_update(self.raw, terminal.raw()).into_result() }
    }

    pub fn cols(&self) -> Result<u16, Error> {
        self.get_u16(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLS)
    }

    pub fn rows(&self) -> Result<u16, Error> {
        self.get_u16(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_ROWS)
    }

    pub fn dirty(&self) -> Result<Dirty, Error> {
        let mut out = ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FALSE;
        // SAFETY: out points to the matching enum storage for the requested data kind.
        unsafe {
            ffi::ghostty_render_state_get(
                self.raw,
                ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_DIRTY,
                (&mut out as *mut ffi::GhosttyRenderStateDirty).cast(),
            )
            .into_result()?;
        }
        Ok(Dirty::from_raw(out))
    }

    pub fn cursor_visible(&self) -> Result<bool, Error> {
        self.get_bool(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VISIBLE)
    }

    pub fn cursor_blinking(&self) -> Result<bool, Error> {
        self.get_bool(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_BLINKING)
    }

    pub fn cursor_visual_style(&self) -> Result<CursorVisualStyle, Error> {
        let mut out: ffi::GhosttyRenderStateCursorVisualStyle = 0;
        // SAFETY: out points to the matching enum storage for the requested data kind.
        unsafe {
            ffi::ghostty_render_state_get(
                self.raw,
                ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VISUAL_STYLE,
                (&mut out as *mut ffi::GhosttyRenderStateCursorVisualStyle).cast(),
            )
            .into_result()?;
        }
        Ok(CursorVisualStyle::from_raw(out))
    }

    pub fn cursor_viewport(&self) -> Result<Option<CursorViewport>, Error> {
        if !self.get_bool(
            ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_HAS_VALUE,
        )? {
            return Ok(None);
        }
        Ok(Some(CursorViewport {
            x: self
                .get_u16(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_X)?,
            y: self
                .get_u16(ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_Y)?,
            wide_tail: self.get_bool(
                ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_WIDE_TAIL,
            )?,
        }))
    }

    pub fn colors(&self) -> Result<RenderColors, Error> {
        let mut colors = ffi::GhosttyRenderStateColors {
            size: mem::size_of::<ffi::GhosttyRenderStateColors>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_render_state_colors_get(self.raw, &mut colors).into_result()?;
        }
        Ok(RenderColors {
            background: colors.background.into(),
            foreground: colors.foreground.into(),
            palette: colors.palette.map(Into::into),
        })
    }

    pub fn set_dirty(&mut self, dirty: Dirty) -> Result<(), Error> {
        let value = dirty.as_raw();
        // SAFETY: value pointer matches the expected option type.
        unsafe {
            ffi::ghostty_render_state_set(
                self.raw,
                ffi::GhosttyRenderStateOption_GHOSTTY_RENDER_STATE_OPTION_DIRTY,
                (&value as *const ffi::GhosttyRenderStateDirty).cast(),
            )
            .into_result()
        }
    }

    pub fn populate_row_iterator<'a>(
        &'a self,
        iterator: &'a mut RowIterator,
    ) -> Result<RowIter<'a>, Error> {
        // SAFETY: iterator raw handle is valid and will not outlive self.
        unsafe {
            ffi::ghostty_render_state_get(
                self.raw,
                ffi::GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR,
                (&mut iterator.raw as *mut ffi::GhosttyRenderStateRowIterator).cast(),
            )
            .into_result()?;
        }
        Ok(RowIter {
            iterator,
            _state: PhantomData,
        })
    }

    fn get_u16(&self, data: ffi::GhosttyRenderStateData) -> Result<u16, Error> {
        let mut out = 0u16;
        // SAFETY: out points to a u16 matching the requested render-state data type.
        unsafe {
            ffi::ghostty_render_state_get(self.raw, data, (&mut out as *mut u16).cast())
                .into_result()?;
        }
        Ok(out)
    }

    fn get_bool(&self, data: ffi::GhosttyRenderStateData) -> Result<bool, Error> {
        let mut out = false;
        unsafe {
            ffi::ghostty_render_state_get(self.raw, data, (&mut out as *mut bool).cast())
                .into_result()?;
        }
        Ok(out)
    }
}

// SAFETY: these opaque handles are only used behind external synchronization in pane runtime.
unsafe impl Send for RenderState {}

impl Drop for RenderState {
    fn drop(&mut self) {
        // SAFETY: freeing a null or live handle is allowed by the C API.
        unsafe {
            ffi::ghostty_render_state_free(self.raw);
        }
    }
}

pub struct KeyEvent {
    raw: ffi::GhosttyKeyEvent,
}

impl KeyEvent {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        unsafe { ffi::ghostty_key_event_new(ptr::null(), &mut raw).into_result()? };
        Ok(Self { raw })
    }

    pub fn set_action(&mut self, action: ffi::GhosttyKeyAction) {
        unsafe { ffi::ghostty_key_event_set_action(self.raw, action) }
    }

    pub fn set_key(&mut self, key: u32) {
        unsafe { ffi::ghostty_key_event_set_key(self.raw, key) }
    }

    pub fn set_mods(&mut self, mods: u16) {
        unsafe { ffi::ghostty_key_event_set_mods(self.raw, mods) }
    }

    pub fn set_utf8(&mut self, text: &str) {
        unsafe {
            ffi::ghostty_key_event_set_utf8(self.raw, text.as_ptr().cast::<c_char>(), text.len())
        }
    }

    pub fn set_unshifted_codepoint(&mut self, codepoint: u32) {
        unsafe { ffi::ghostty_key_event_set_unshifted_codepoint(self.raw, codepoint) }
    }
}

impl Drop for KeyEvent {
    fn drop(&mut self) {
        unsafe { ffi::ghostty_key_event_free(self.raw) }
    }
}

pub struct KeyEncoder {
    raw: ffi::GhosttyKeyEncoder,
}

impl KeyEncoder {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        unsafe { ffi::ghostty_key_encoder_new(ptr::null(), &mut raw).into_result()? };
        Ok(Self { raw })
    }

    pub fn set_from_terminal(&mut self, terminal: &Terminal) {
        unsafe { ffi::ghostty_key_encoder_setopt_from_terminal(self.raw, terminal.raw()) }
    }

    pub fn encode(&mut self, event: &KeyEvent) -> Result<Vec<u8>, Error> {
        encode_with_retry(|buf, len, out_len| unsafe {
            ffi::ghostty_key_encoder_encode(self.raw, event.raw, buf, len, out_len)
        })
    }
}

// SAFETY: the opaque encoder handle is only used behind external synchronization in pane runtime.
unsafe impl Send for KeyEncoder {}

impl Drop for KeyEncoder {
    fn drop(&mut self) {
        unsafe { ffi::ghostty_key_encoder_free(self.raw) }
    }
}

pub struct MouseEvent {
    raw: ffi::GhosttyMouseEvent,
}

impl MouseEvent {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        unsafe { ffi::ghostty_mouse_event_new(ptr::null(), &mut raw).into_result()? };
        Ok(Self { raw })
    }

    pub fn set_action(&mut self, action: ffi::GhosttyMouseAction) {
        unsafe { ffi::ghostty_mouse_event_set_action(self.raw, action) }
    }

    pub fn set_button(&mut self, button: ffi::GhosttyMouseButton) {
        unsafe { ffi::ghostty_mouse_event_set_button(self.raw, button) }
    }

    pub fn clear_button(&mut self) {
        unsafe { ffi::ghostty_mouse_event_clear_button(self.raw) }
    }

    pub fn set_mods(&mut self, mods: u16) {
        unsafe { ffi::ghostty_mouse_event_set_mods(self.raw, mods) }
    }

    pub fn set_position(&mut self, x: f32, y: f32) {
        unsafe {
            ffi::ghostty_mouse_event_set_position(self.raw, ffi::GhosttyMousePosition { x, y })
        }
    }
}

impl Drop for MouseEvent {
    fn drop(&mut self) {
        unsafe { ffi::ghostty_mouse_event_free(self.raw) }
    }
}

pub struct MouseEncoder {
    raw: ffi::GhosttyMouseEncoder,
}

impl MouseEncoder {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        unsafe { ffi::ghostty_mouse_encoder_new(ptr::null(), &mut raw).into_result()? };
        Ok(Self { raw })
    }

    pub fn set_from_terminal(&mut self, terminal: &Terminal) {
        unsafe { ffi::ghostty_mouse_encoder_setopt_from_terminal(self.raw, terminal.raw()) }
    }

    pub fn set_size(
        &mut self,
        screen_width: u32,
        screen_height: u32,
        cell_width: u32,
        cell_height: u32,
    ) {
        let size = ffi::GhosttyMouseEncoderSize {
            size: std::mem::size_of::<ffi::GhosttyMouseEncoderSize>(),
            screen_width,
            screen_height,
            cell_width,
            cell_height,
            padding_top: 0,
            padding_bottom: 0,
            padding_right: 0,
            padding_left: 0,
        };
        unsafe {
            ffi::ghostty_mouse_encoder_setopt(
                self.raw,
                ffi::GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_SIZE,
                (&size as *const ffi::GhosttyMouseEncoderSize).cast(),
            )
        }
    }

    pub fn set_format(&mut self, format: ffi::GhosttyMouseFormat) {
        unsafe {
            ffi::ghostty_mouse_encoder_setopt(
                self.raw,
                ffi::GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_FORMAT,
                (&format as *const ffi::GhosttyMouseFormat).cast(),
            )
        }
    }

    pub fn encode(&mut self, event: &MouseEvent) -> Result<Vec<u8>, Error> {
        encode_with_retry(|buf, len, out_len| unsafe {
            ffi::ghostty_mouse_encoder_encode(self.raw, event.raw, buf, len, out_len)
        })
    }
}

impl Drop for MouseEncoder {
    fn drop(&mut self) {
        unsafe { ffi::ghostty_mouse_encoder_free(self.raw) }
    }
}

fn encode_with_retry(
    mut encode: impl FnMut(*mut c_char, usize, *mut usize) -> ffi::GhosttyResult,
) -> Result<Vec<u8>, Error> {
    let mut required = 0usize;
    let result = encode(ptr::null_mut(), 0, &mut required);
    if result != ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE {
        result.into_result()?;
    }
    let mut buffer = vec![0u8; required.max(16)];
    let mut written = 0usize;
    encode(
        buffer.as_mut_ptr().cast::<c_char>(),
        buffer.len(),
        &mut written,
    )
    .into_result()?;
    buffer.truncate(written);
    Ok(buffer)
}

pub struct RowIterator {
    raw: ffi::GhosttyRenderStateRowIterator,
}

impl RowIterator {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        // SAFETY: valid out pointer and null allocator use default allocator.
        unsafe {
            ffi::ghostty_render_state_row_iterator_new(ptr::null(), &mut raw).into_result()?;
        }
        Ok(Self { raw })
    }
}

// SAFETY: these opaque handles are only used behind external synchronization in pane runtime.
unsafe impl Send for RowIterator {}

impl Drop for RowIterator {
    fn drop(&mut self) {
        // SAFETY: freeing a null or live handle is allowed by the C API.
        unsafe {
            ffi::ghostty_render_state_row_iterator_free(self.raw);
        }
    }
}

pub struct RowIter<'a> {
    iterator: &'a mut RowIterator,
    _state: PhantomData<&'a RenderState>,
}

impl<'a> RowIter<'a> {
    pub fn next(&mut self) -> bool {
        // SAFETY: iterator handle is valid while self is alive.
        unsafe { ffi::ghostty_render_state_row_iterator_next(self.iterator.raw) }
    }

    pub fn dirty(&self) -> Result<bool, Error> {
        let mut dirty = false;
        // SAFETY: dirty output matches requested row data type.
        unsafe {
            ffi::ghostty_render_state_row_get(
                self.iterator.raw,
                ffi::GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_DIRTY,
                (&mut dirty as *mut bool).cast(),
            )
            .into_result()?;
        }
        Ok(dirty)
    }

    #[cfg(windows)]
    pub fn wrap_state(&self) -> Result<(bool, bool), Error> {
        let mut row = 0;
        // SAFETY: row output matches requested row data type.
        unsafe {
            ffi::ghostty_render_state_row_get(
                self.iterator.raw,
                ffi::GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_RAW,
                (&mut row as *mut ffi::GhosttyRow).cast(),
            )
            .into_result()?;
        }
        let mut soft_wrapped = false;
        // SAFETY: wrap output matches requested row data type.
        unsafe {
            ffi::ghostty_row_get(
                row,
                ffi::GhosttyRowData_GHOSTTY_ROW_DATA_WRAP,
                (&mut soft_wrapped as *mut bool).cast(),
            )
            .into_result()?;
        }
        let mut wrap_continuation = false;
        // SAFETY: wrap continuation output matches requested row data type.
        unsafe {
            ffi::ghostty_row_get(
                row,
                ffi::GhosttyRowData_GHOSTTY_ROW_DATA_WRAP_CONTINUATION,
                (&mut wrap_continuation as *mut bool).cast(),
            )
            .into_result()?;
        }
        Ok((soft_wrapped, wrap_continuation))
    }

    pub fn clear_dirty(&mut self) -> Result<(), Error> {
        self.set_dirty(false)
    }

    pub fn set_dirty(&mut self, dirty: bool) -> Result<(), Error> {
        // SAFETY: dirty pointer matches the expected row option type.
        unsafe {
            ffi::ghostty_render_state_row_set(
                self.iterator.raw,
                ffi::GhosttyRenderStateRowOption_GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY,
                (&dirty as *const bool).cast(),
            )
            .into_result()
        }
    }

    pub fn selection(&self) -> Result<Option<RowSelection>, Error> {
        let mut selection = ffi::GhosttyRenderStateRowSelection {
            size: mem::size_of::<ffi::GhosttyRenderStateRowSelection>(),
            ..Default::default()
        };
        let result = unsafe {
            ffi::ghostty_render_state_row_get(
                self.iterator.raw,
                ffi::GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION,
                (&mut selection as *mut ffi::GhosttyRenderStateRowSelection).cast(),
            )
        };
        match result {
            ffi::GhosttyResult_GHOSTTY_SUCCESS => Ok(Some(RowSelection {
                start_x: selection.start_x,
                end_x: selection.end_x,
            })),
            ffi::GhosttyResult_GHOSTTY_NO_VALUE => Ok(None),
            other => Err(Error(other)),
        }
    }

    pub fn populate_cells<'b>(
        &'b mut self,
        cells: &'b mut RowCells,
    ) -> Result<RowCellIter<'b>, Error> {
        // SAFETY: cells raw handle is valid and will not outlive the current row borrow.
        unsafe {
            ffi::ghostty_render_state_row_get(
                self.iterator.raw,
                ffi::GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_CELLS,
                (&mut cells.raw as *mut ffi::GhosttyRenderStateRowCells).cast(),
            )
            .into_result()?;
        }
        Ok(RowCellIter { cells })
    }
}

pub struct RowCells {
    raw: ffi::GhosttyRenderStateRowCells,
}

impl RowCells {
    pub fn new() -> Result<Self, Error> {
        let mut raw = ptr::null_mut();
        // SAFETY: valid out pointer and null allocator use default allocator.
        unsafe {
            ffi::ghostty_render_state_row_cells_new(ptr::null(), &mut raw).into_result()?;
        }
        Ok(Self { raw })
    }
}

// SAFETY: these opaque handles are only used behind external synchronization in pane runtime.
unsafe impl Send for RowCells {}

impl Drop for RowCells {
    fn drop(&mut self) {
        // SAFETY: freeing a null or live handle is allowed by the C API.
        unsafe {
            ffi::ghostty_render_state_row_cells_free(self.raw);
        }
    }
}

pub struct RowCellIter<'a> {
    cells: &'a mut RowCells,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CellBasicData {
    pub wide: CellWide,
    pub has_hyperlink: bool,
    pub has_styling: bool,
    pub style: CellStyle,
}

impl Default for CellBasicData {
    fn default() -> Self {
        Self {
            wide: CellWide::Narrow,
            has_hyperlink: false,
            has_styling: false,
            style: CellStyle::default(),
        }
    }
}

impl<'a> RowCellIter<'a> {
    pub fn next(&mut self) -> bool {
        // SAFETY: cells handle is valid while self is alive.
        unsafe { ffi::ghostty_render_state_row_cells_next(self.cells.raw) }
    }

    pub fn select(&mut self, x: u16) -> Result<(), Error> {
        unsafe { ffi::ghostty_render_state_row_cells_select(self.cells.raw, x).into_result() }
    }

    fn raw_cell(&self) -> Result<ffi::GhosttyCell, Error> {
        let mut raw = ffi::GhosttyCell::default();
        unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW,
                (&mut raw as *mut ffi::GhosttyCell).cast(),
            )
            .into_result()?;
        }
        Ok(raw)
    }

    pub fn basic_data(&self) -> Result<CellBasicData, Error> {
        let mut raw = ffi::GhosttyCell::default();
        let mut style = ffi::GhosttyStyle {
            size: mem::size_of::<ffi::GhosttyStyle>(),
            ..Default::default()
        };
        let mut has_styling = false;
        let row_keys = [
            ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW,
            ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE,
            ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_HAS_STYLING,
        ];
        let mut row_values = [
            (&mut raw as *mut ffi::GhosttyCell).cast::<c_void>(),
            (&mut style as *mut ffi::GhosttyStyle).cast::<c_void>(),
            (&mut has_styling as *mut bool).cast::<c_void>(),
        ];
        let mut written = 0usize;
        unsafe {
            ffi::ghostty_render_state_row_cells_get_multi(
                self.cells.raw,
                row_keys.len(),
                row_keys.as_ptr(),
                row_values.as_mut_ptr(),
                &mut written,
            )
            .into_result()?;
        }

        let mut wide = ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_NARROW;
        let mut has_hyperlink = false;
        let cell_keys = [
            ffi::GhosttyCellData_GHOSTTY_CELL_DATA_WIDE,
            ffi::GhosttyCellData_GHOSTTY_CELL_DATA_HAS_HYPERLINK,
        ];
        let mut cell_values = [
            (&mut wide as *mut ffi::GhosttyCellWide).cast::<c_void>(),
            (&mut has_hyperlink as *mut bool).cast::<c_void>(),
        ];
        unsafe {
            ffi::ghostty_cell_get_multi(
                raw,
                cell_keys.len(),
                cell_keys.as_ptr(),
                cell_values.as_mut_ptr(),
                &mut written,
            )
            .into_result()?;
        }

        Ok(CellBasicData {
            wide: CellWide::from_raw(wide),
            has_hyperlink,
            has_styling,
            style: style.into(),
        })
    }

    pub fn wide(&self) -> Result<CellWide, Error> {
        let raw = self.raw_cell()?;
        let mut wide = ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_NARROW;
        unsafe {
            ffi::ghostty_cell_get(
                raw,
                ffi::GhosttyCellData_GHOSTTY_CELL_DATA_WIDE,
                (&mut wide as *mut ffi::GhosttyCellWide).cast(),
            )
            .into_result()?;
        }
        Ok(CellWide::from_raw(wide))
    }

    pub fn has_hyperlink(&self) -> Result<bool, Error> {
        let raw = self.raw_cell()?;
        let mut has_hyperlink = false;
        unsafe {
            ffi::ghostty_cell_get(
                raw,
                ffi::GhosttyCellData_GHOSTTY_CELL_DATA_HAS_HYPERLINK,
                (&mut has_hyperlink as *mut bool).cast(),
            )
            .into_result()?;
        }
        Ok(has_hyperlink)
    }

    pub fn style(&self) -> Result<CellStyle, Error> {
        let mut style = ffi::GhosttyStyle {
            size: mem::size_of::<ffi::GhosttyStyle>(),
            ..Default::default()
        };
        unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE,
                (&mut style as *mut ffi::GhosttyStyle).cast(),
            )
            .into_result()?;
        }
        Ok(style.into())
    }

    pub fn content_bg_color(&self) -> Result<Option<CellColor>, Error> {
        let raw = self.raw_cell()?;
        let mut tag = ffi::GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_CODEPOINT;
        unsafe {
            ffi::ghostty_cell_get(
                raw,
                ffi::GhosttyCellData_GHOSTTY_CELL_DATA_CONTENT_TAG,
                (&mut tag as *mut ffi::GhosttyCellContentTag).cast(),
            )
            .into_result()?;
        }

        match tag {
            ffi::GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_BG_COLOR_PALETTE => {
                let mut index = 0u8;
                unsafe {
                    ffi::ghostty_cell_get(
                        raw,
                        ffi::GhosttyCellData_GHOSTTY_CELL_DATA_COLOR_PALETTE,
                        (&mut index as *mut u8).cast(),
                    )
                    .into_result()?;
                }
                Ok(Some(CellColor::Palette(index)))
            }
            ffi::GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_BG_COLOR_RGB => {
                let mut color = ffi::GhosttyColorRgb::default();
                unsafe {
                    ffi::ghostty_cell_get(
                        raw,
                        ffi::GhosttyCellData_GHOSTTY_CELL_DATA_COLOR_RGB,
                        (&mut color as *mut ffi::GhosttyColorRgb).cast(),
                    )
                    .into_result()?;
                }
                Ok(Some(CellColor::Rgb(color.into())))
            }
            _ => Ok(None),
        }
    }

    pub fn fg_color(&self) -> Result<Option<RgbColor>, Error> {
        let mut color = ffi::GhosttyColorRgb::default();
        let result = unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR,
                (&mut color as *mut ffi::GhosttyColorRgb).cast(),
            )
        };
        match result {
            ffi::GhosttyResult_GHOSTTY_SUCCESS => Ok(Some(color.into())),
            ffi::GhosttyResult_GHOSTTY_INVALID_VALUE => Ok(None),
            other => Err(Error(other)),
        }
    }

    pub fn bg_color(&self) -> Result<Option<RgbColor>, Error> {
        let mut color = ffi::GhosttyColorRgb::default();
        let result = unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR,
                (&mut color as *mut ffi::GhosttyColorRgb).cast(),
            )
        };
        match result {
            ffi::GhosttyResult_GHOSTTY_SUCCESS => Ok(Some(color.into())),
            ffi::GhosttyResult_GHOSTTY_INVALID_VALUE => Ok(None),
            other => Err(Error(other)),
        }
    }

    fn raw_cell_text_into(&self, text: &mut String) -> Result<(), Error> {
        let raw = self.raw_cell()?;
        let mut has_text = false;
        unsafe {
            ffi::ghostty_cell_get(
                raw,
                ffi::GhosttyCellData_GHOSTTY_CELL_DATA_HAS_TEXT,
                (&mut has_text as *mut bool).cast(),
            )
            .into_result()?;
        }
        if !has_text {
            return Ok(());
        }

        let mut codepoint = 0u32;
        unsafe {
            ffi::ghostty_cell_get(
                raw,
                ffi::GhosttyCellData_GHOSTTY_CELL_DATA_CODEPOINT,
                (&mut codepoint as *mut u32).cast(),
            )
            .into_result()?;
        }
        if let Some(ch) = char::from_u32(codepoint) {
            text.push(ch);
        }
        Ok(())
    }

    pub fn grapheme_text(&self) -> Result<String, Error> {
        let mut bytes = Vec::new();
        let mut text = String::new();
        self.grapheme_text_into(&mut bytes, &mut text)?;
        Ok(text)
    }

    pub fn grapheme_text_into(&self, bytes: &mut Vec<u8>, text: &mut String) -> Result<(), Error> {
        text.clear();
        bytes.clear();

        let mut buffer = ffi::GhosttyBuffer {
            ptr: ptr::null_mut(),
            cap: 0,
            len: 0,
        };
        let result = unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8,
                (&mut buffer as *mut ffi::GhosttyBuffer).cast(),
            )
        };
        match result {
            ffi::GhosttyResult_GHOSTTY_SUCCESS if buffer.len == 0 => {
                return self.raw_cell_text_into(text);
            }
            ffi::GhosttyResult_GHOSTTY_SUCCESS => {
                return Err(Error(ffi::GhosttyResult_GHOSTTY_INVALID_VALUE));
            }
            ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE => {}
            other => return Err(Error(other)),
        }

        if buffer.len == 0 {
            return self.raw_cell_text_into(text);
        }
        bytes.resize(buffer.len, 0);
        let mut buffer = ffi::GhosttyBuffer {
            ptr: bytes.as_mut_ptr(),
            cap: bytes.len(),
            len: 0,
        };
        unsafe {
            ffi::ghostty_render_state_row_cells_get(
                self.cells.raw,
                ffi::GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8,
                (&mut buffer as *mut ffi::GhosttyBuffer).cast(),
            )
            .into_result()?;
        }
        if buffer.len > bytes.len() {
            return Err(Error(ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE));
        }
        bytes.truncate(buffer.len);
        match std::str::from_utf8(bytes) {
            Ok(value) => text.push_str(value),
            Err(_) => text.push_str(&String::from_utf8_lossy(bytes)),
        }
        Ok(())
    }
}

