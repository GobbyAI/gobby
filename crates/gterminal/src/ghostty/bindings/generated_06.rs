impl Default for GhosttyFormatterTerminalOptions {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
unsafe extern "C" {
    #[doc = " Create a formatter for a terminal's active screen.\n\n The terminal must outlive the formatter. The formatter stores a borrowed\n reference to the terminal and reads its current state on each format call.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param formatter Pointer to store the created formatter handle\n @param terminal The terminal to format (must not be NULL)\n @param options Formatting options\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup formatter"]
    pub fn ghostty_formatter_terminal_new(
        allocator: *const GhosttyAllocator,
        formatter: *mut GhosttyFormatter,
        terminal: GhosttyTerminal,
        options: GhosttyFormatterTerminalOptions,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Run the formatter and stream output to a writer.\n\n Each call formats the current terminal state and invokes the writer\n synchronously as output becomes available. The callback may be called more\n than once and must not call formatter or terminal APIs using the same\n formatter or its terminal.\n\n If an error occurs, the writer may already contain a partial formatted\n output. The operation cannot be resumed from that partial output. This\n function does not flush or make the caller's destination durable.\n\n @param formatter The formatter handle (must not be NULL)\n @param writer Destination writer whose write callback must not be NULL\n @return GHOSTTY_SUCCESS on success, GHOSTTY_IO_ERROR if the writer rejects\n         output, GHOSTTY_LIMIT_EXCEEDED if output accounting overflows, or\n         GHOSTTY_INVALID_VALUE if an argument is invalid\n\n @ingroup formatter"]
    pub fn ghostty_formatter_format(
        formatter: GhosttyFormatter,
        writer: GhosttyWriter,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Run the formatter and produce output into the caller-provided buffer.\n\n Each call formats the current terminal state. Pass NULL for buf to\n query the required buffer size without writing any output; in that case\n out_written receives the required size and the return value is\n GHOSTTY_OUT_OF_SPACE.\n\n If the buffer is too small, returns GHOSTTY_OUT_OF_SPACE and sets\n out_written to the required size. The caller can then retry with a\n larger buffer.\n\n @param formatter The formatter handle (must not be NULL)\n @param buf Pointer to the output buffer, or NULL to query size\n @param buf_len Length of the output buffer in bytes\n @param out_written Pointer to receive the number of bytes written,\n                    or the required size on failure\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup formatter"]
    pub fn ghostty_formatter_format_buf(
        formatter: GhosttyFormatter,
        buf: *mut u8,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Run the formatter and return an allocated buffer with the output.\n\n Each call formats the current terminal state. The buffer is allocated\n using the provided allocator (or the default allocator if NULL).\n The caller is responsible for freeing the returned buffer with\n ghostty_free(), passing the same allocator (or NULL for the default)\n that was used for the allocation.\n Empty output returns GHOSTTY_SUCCESS with *out_ptr set to NULL and\n *out_len set to zero. This result can be passed to ghostty_free().\n\n @param formatter The formatter handle (must not be NULL)\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param out_ptr Pointer to receive the allocated buffer\n @param out_len Pointer to receive the length of the output in bytes\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n         failure\n\n @ingroup formatter"]
    pub fn ghostty_formatter_format_alloc(
        formatter: GhosttyFormatter,
        allocator: *const GhosttyAllocator,
        out_ptr: *mut *mut u8,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a formatter instance.\n\n Releases all resources associated with the formatter. After this call,\n the formatter handle becomes invalid.\n\n @param formatter The formatter handle to free (may be NULL)\n\n @ingroup formatter"]
    pub fn ghostty_formatter_free(formatter: GhosttyFormatter);
}
#[doc = " Not dirty at all; rendering can be skipped."]
pub const GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FALSE: GhosttyRenderStateDirty = 0;
#[doc = " Some rows changed; renderer can redraw incrementally."]
pub const GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_PARTIAL: GhosttyRenderStateDirty = 1;
#[doc = " Global state changed; renderer should redraw everything."]
pub const GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FULL: GhosttyRenderStateDirty = 2;
#[doc = " Global state changed; renderer should redraw everything."]
pub const GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_MAX_VALUE: GhosttyRenderStateDirty =
    2147483647;
#[doc = " Dirty state of a render state after update.\n\n @ingroup render"]
pub type GhosttyRenderStateDirty = ::std::os::raw::c_int;
#[doc = " Bar cursor (DECSCUSR 5, 6)."]
pub const GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BAR:
    GhosttyRenderStateCursorVisualStyle = 0;
#[doc = " Block cursor (DECSCUSR 1, 2)."]
pub const GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK:
    GhosttyRenderStateCursorVisualStyle = 1;
#[doc = " Underline cursor (DECSCUSR 3, 4)."]
pub const GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE:
    GhosttyRenderStateCursorVisualStyle = 2;
#[doc = " Hollow block cursor."]
pub const GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW : GhosttyRenderStateCursorVisualStyle = 3 ;
#[doc = " Hollow block cursor."]
pub const GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_MAX_VALUE:
    GhosttyRenderStateCursorVisualStyle = 2147483647;
#[doc = " Visual style of the cursor.\n\n @ingroup render"]
pub type GhosttyRenderStateCursorVisualStyle = ::std::os::raw::c_int;
#[doc = " Invalid / sentinel value."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_INVALID: GhosttyRenderStateData = 0;
#[doc = " Viewport width in cells (uint16_t)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLS: GhosttyRenderStateData = 1;
#[doc = " Viewport height in cells (uint16_t)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_ROWS: GhosttyRenderStateData = 2;
#[doc = " Current dirty state (GhosttyRenderStateDirty)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_DIRTY: GhosttyRenderStateData = 3;
#[doc = " Populate a pre-allocated GhosttyRenderStateRowIterator with row data\n  from the render state (GhosttyRenderStateRowIterator). Row data is\n  only valid as long as the underlying render state is not updated.\n  It is unsafe to use row data after updating the render state."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR: GhosttyRenderStateData = 4;
#[doc = " Default/current background color (GhosttyColorRgb)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLOR_BACKGROUND:
    GhosttyRenderStateData = 5;
#[doc = " Default/current foreground color (GhosttyColorRgb)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLOR_FOREGROUND:
    GhosttyRenderStateData = 6;
#[doc = " Cursor color when explicitly set by terminal state (GhosttyColorRgb).\n  Returns GHOSTTY_INVALID_VALUE if no explicit cursor color is set;\n  use COLOR_CURSOR_HAS_VALUE to check first."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLOR_CURSOR: GhosttyRenderStateData = 7;
#[doc = " Whether an explicit cursor color is set (bool)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLOR_CURSOR_HAS_VALUE:
    GhosttyRenderStateData = 8;
#[doc = " The active 256-color palette (GhosttyColorRgb[256])."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLOR_PALETTE: GhosttyRenderStateData =
    9;
#[doc = " The visual style of the cursor (GhosttyRenderStateCursorVisualStyle)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VISUAL_STYLE:
    GhosttyRenderStateData = 10;
#[doc = " Whether the cursor is visible based on terminal modes (bool)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VISIBLE: GhosttyRenderStateData =
    11;
#[doc = " Whether the cursor should blink based on terminal modes (bool)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_BLINKING: GhosttyRenderStateData =
    12;
#[doc = " Whether the cursor is at a password input field (bool)."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_PASSWORD_INPUT:
    GhosttyRenderStateData = 13;
#[doc = " Whether the cursor is visible within the viewport (bool).\n  If false, the cursor viewport position values are undefined."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_HAS_VALUE:
    GhosttyRenderStateData = 14;
#[doc = " Cursor viewport x position in cells (uint16_t).\n  Only valid when CURSOR_VIEWPORT_HAS_VALUE is true."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_X:
    GhosttyRenderStateData = 15;
#[doc = " Cursor viewport y position in cells (uint16_t).\n  Only valid when CURSOR_VIEWPORT_HAS_VALUE is true."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_Y:
    GhosttyRenderStateData = 16;
#[doc = " Whether the cursor is on the tail of a wide character (bool).\n  Only valid when CURSOR_VIEWPORT_HAS_VALUE is true."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR_VIEWPORT_WIDE_TAIL:
    GhosttyRenderStateData = 17;
#[doc = " All cursor state in one sized struct (GhosttyRenderStateCursor).\n  Initialize the output with GHOSTTY_INIT_SIZED before querying."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_CURSOR: GhosttyRenderStateData = 18;
#[doc = " All render-state colors in one sized struct (GhosttyRenderStateColors).\n  Initialize the output with GHOSTTY_INIT_SIZED before querying."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_COLORS: GhosttyRenderStateData = 19;
#[doc = " All render-state colors in one sized struct (GhosttyRenderStateColors).\n  Initialize the output with GHOSTTY_INIT_SIZED before querying."]
pub const GhosttyRenderStateData_GHOSTTY_RENDER_STATE_DATA_MAX_VALUE: GhosttyRenderStateData =
    2147483647;
#[doc = " Queryable data kinds for ghostty_render_state_get().\n\n @ingroup render"]
pub type GhosttyRenderStateData = ::std::os::raw::c_int;
#[doc = " Set dirty state (GhosttyRenderStateDirty)."]
pub const GhosttyRenderStateOption_GHOSTTY_RENDER_STATE_OPTION_DIRTY: GhosttyRenderStateOption = 0;
#[doc = " Set dirty state (GhosttyRenderStateDirty)."]
pub const GhosttyRenderStateOption_GHOSTTY_RENDER_STATE_OPTION_MAX_VALUE: GhosttyRenderStateOption =
    2147483647;
#[doc = " Settable options for ghostty_render_state_set().\n\n @ingroup render"]
pub type GhosttyRenderStateOption = ::std::os::raw::c_int;
#[doc = " Invalid / sentinel value."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_INVALID:
    GhosttyRenderStateRowData = 0;
#[doc = " Whether the current row is dirty (bool)."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_DIRTY: GhosttyRenderStateRowData =
    1;
#[doc = " The raw row value (GhosttyRow)."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_RAW: GhosttyRenderStateRowData =
    2;
#[doc = " Populate a pre-allocated GhosttyRenderStateRowCells with cell data for\n  the current row (GhosttyRenderStateRowCells). Cell data is only\n  valid as long as the underlying render state is not updated.\n  It is unsafe to use cell data after updating the render state."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_CELLS: GhosttyRenderStateRowData =
    3;
#[doc = " Row-local selected cell range (GhosttyRenderStateRowSelection)."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION:
    GhosttyRenderStateRowData = 4;
#[doc = " A borrowed view of the raw cell values for the current row\n  (GhosttyCellsView). One value per column, identical to querying\n  GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW for each cell. The view\n  is only valid as long as the underlying render state is not\n  updated; it is unsafe to use after updating the render state.\n\n  This is the bulk alternative to iterating cells one at a time.\n  It lets callers with expensive call boundaries (e.g. WebAssembly\n  embedders) read an entire row with a single call.\n\n  Bit positions aren't protected by ABI, so callers should parse them\n  out of the manifest from `ghostty_type_json`. Callers with access\n  to the C header or without high FFI costs should use `ghostty_cell_get`."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_CELLS_RAW:
    GhosttyRenderStateRowData = 5;
#[doc = " A borrowed view of the raw cell values for the current row\n  (GhosttyCellsView). One value per column, identical to querying\n  GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW for each cell. The view\n  is only valid as long as the underlying render state is not\n  updated; it is unsafe to use after updating the render state.\n\n  This is the bulk alternative to iterating cells one at a time.\n  It lets callers with expensive call boundaries (e.g. WebAssembly\n  embedders) read an entire row with a single call.\n\n  Bit positions aren't protected by ABI, so callers should parse them\n  out of the manifest from `ghostty_type_json`. Callers with access\n  to the C header or without high FFI costs should use `ghostty_cell_get`."]
pub const GhosttyRenderStateRowData_GHOSTTY_RENDER_STATE_ROW_DATA_MAX_VALUE:
    GhosttyRenderStateRowData = 2147483647;
#[doc = " Queryable data kinds for ghostty_render_state_row_get().\n\n @ingroup render"]
pub type GhosttyRenderStateRowData = ::std::os::raw::c_int;
#[doc = " Set dirty state for the current row (bool)."]
pub const GhosttyRenderStateRowOption_GHOSTTY_RENDER_STATE_ROW_OPTION_DIRTY:
    GhosttyRenderStateRowOption = 0;
#[doc = " Set dirty state for the current row (bool)."]
pub const GhosttyRenderStateRowOption_GHOSTTY_RENDER_STATE_ROW_OPTION_MAX_VALUE:
    GhosttyRenderStateRowOption = 2147483647;
#[doc = " Settable options for ghostty_render_state_row_set().\n\n @ingroup render"]
pub type GhosttyRenderStateRowOption = ::std::os::raw::c_int;
#[doc = " Row-local selection range.\n\n This struct uses the sized-struct ABI pattern. Initialize with\n GHOSTTY_INIT_SIZED(GhosttyRenderStateRowSelection) before querying\n GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION.\n\n Querying GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION returns GHOSTTY_NO_VALUE\n if the current row does not intersect the current selection.\n\n @ingroup render"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyRenderStateRowSelection {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyRenderStateRowSelection)."]
    pub size: usize,
    #[doc = " Start column of the row-local selection range, inclusive."]
    pub start_x: u16,
    #[doc = " End column of the row-local selection range, inclusive."]
    pub end_x: u16,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyRenderStateRowSelection"]
        [::std::mem::size_of::<GhosttyRenderStateRowSelection>() - 16usize];
    ["Alignment of GhosttyRenderStateRowSelection"]
        [::std::mem::align_of::<GhosttyRenderStateRowSelection>() - 8usize];
    ["Offset of field: GhosttyRenderStateRowSelection::size"]
        [::std::mem::offset_of!(GhosttyRenderStateRowSelection, size) - 0usize];
    ["Offset of field: GhosttyRenderStateRowSelection::start_x"]
        [::std::mem::offset_of!(GhosttyRenderStateRowSelection, start_x) - 8usize];
    ["Offset of field: GhosttyRenderStateRowSelection::end_x"]
        [::std::mem::offset_of!(GhosttyRenderStateRowSelection, end_x) - 10usize];
};
#[doc = " Render-state cursor information.\n\n This struct uses the sized-struct ABI pattern. Initialize with\n GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor) before querying\n GHOSTTY_RENDER_STATE_DATA_CURSOR.\n\n When viewport_has_value is false, viewport_x, viewport_y, and wide_tail\n contain undefined data and must not be read.\n\n @ingroup render"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyRenderStateCursor {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyRenderStateCursor)."]
    pub size: usize,
    #[doc = " Whether the cursor is visible within the viewport."]
    pub viewport_has_value: bool,
    #[doc = " Cursor viewport x position in cells."]
    pub viewport_x: u16,
    #[doc = " Cursor viewport y position in cells."]
    pub viewport_y: u16,
    #[doc = " Whether the cursor is on the tail of a wide character."]
    pub wide_tail: bool,
    #[doc = " Whether the cursor is visible based on terminal modes."]
    pub visible: bool,
    #[doc = " Whether the cursor should blink based on terminal modes."]
    pub blinking: bool,
    #[doc = " Whether the cursor is at a password input field."]
    pub password_input: bool,
    #[doc = " The visual style of the cursor."]
    pub visual_style: GhosttyRenderStateCursorVisualStyle,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyRenderStateCursor"]
        [::std::mem::size_of::<GhosttyRenderStateCursor>() - 24usize];
    ["Alignment of GhosttyRenderStateCursor"]
        [::std::mem::align_of::<GhosttyRenderStateCursor>() - 8usize];
    ["Offset of field: GhosttyRenderStateCursor::size"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, size) - 0usize];
    ["Offset of field: GhosttyRenderStateCursor::viewport_has_value"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, viewport_has_value) - 8usize];
    ["Offset of field: GhosttyRenderStateCursor::viewport_x"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, viewport_x) - 10usize];
    ["Offset of field: GhosttyRenderStateCursor::viewport_y"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, viewport_y) - 12usize];
    ["Offset of field: GhosttyRenderStateCursor::wide_tail"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, wide_tail) - 14usize];
    ["Offset of field: GhosttyRenderStateCursor::visible"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, visible) - 15usize];
    ["Offset of field: GhosttyRenderStateCursor::blinking"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, blinking) - 16usize];
    ["Offset of field: GhosttyRenderStateCursor::password_input"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, password_input) - 17usize];
    ["Offset of field: GhosttyRenderStateCursor::visual_style"]
        [::std::mem::offset_of!(GhosttyRenderStateCursor, visual_style) - 20usize];
};
impl Default for GhosttyRenderStateCursor {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Render-state color information.\n\n This struct uses the sized-struct ABI pattern. Initialize with\n GHOSTTY_INIT_SIZED(GhosttyRenderStateColors) before querying\n GHOSTTY_RENDER_STATE_DATA_COLORS.\n\n Example:\n @code\n GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);\n GhosttyResult result = ghostty_render_state_get(\n     state, GHOSTTY_RENDER_STATE_DATA_COLORS, &colors);\n @endcode\n\n @ingroup render"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyRenderStateColors {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyRenderStateColors)."]
    pub size: usize,
    #[doc = " The default/current background color for the render state."]
    pub background: GhosttyColorRgb,
    #[doc = " The default/current foreground color for the render state."]
    pub foreground: GhosttyColorRgb,
    #[doc = " The cursor color when explicitly set by terminal state."]
    pub cursor: GhosttyColorRgb,
    #[doc = " True when cursor contains a valid explicit cursor color value.\n If this is false, the cursor color should be ignored; it will\n contain undefined data."]
    pub cursor_has_value: bool,
    #[doc = " The active 256-color palette for this render state."]
    pub palette: [GhosttyColorRgb; 256usize],
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyRenderStateColors"]
        [::std::mem::size_of::<GhosttyRenderStateColors>() - 792usize];
    ["Alignment of GhosttyRenderStateColors"]
        [::std::mem::align_of::<GhosttyRenderStateColors>() - 8usize];
    ["Offset of field: GhosttyRenderStateColors::size"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, size) - 0usize];
    ["Offset of field: GhosttyRenderStateColors::background"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, background) - 8usize];
    ["Offset of field: GhosttyRenderStateColors::foreground"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, foreground) - 11usize];
    ["Offset of field: GhosttyRenderStateColors::cursor"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, cursor) - 14usize];
    ["Offset of field: GhosttyRenderStateColors::cursor_has_value"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, cursor_has_value) - 17usize];
    ["Offset of field: GhosttyRenderStateColors::palette"]
        [::std::mem::offset_of!(GhosttyRenderStateColors, palette) - 18usize];
};
impl Default for GhosttyRenderStateColors {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
unsafe extern "C" {
    #[doc = " Create a new render state instance.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param state Pointer to store the created render state handle\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n failure\n\n @ingroup render"]
    pub fn ghostty_render_state_new(
        allocator: *const GhosttyAllocator,
        state: *mut GhosttyRenderState,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a render state instance.\n\n Releases all resources associated with the render state. After this call,\n the render state handle becomes invalid.\n\n @param state The render state handle to free (may be NULL)\n\n @ingroup render"]
    pub fn ghostty_render_state_free(state: GhosttyRenderState);
}
unsafe extern "C" {
    #[doc = " Update a render state instance from a terminal.\n\n This consumes terminal/screen dirty state in the same way as the internal\n render state update path.\n\n This is a convenience function that performs a full update in one call,\n equivalent to ghostty_render_state_begin_update immediately followed by\n ghostty_render_state_end_update. Callers that hold a lock over the\n terminal state should prefer calling the two phases directly so that the\n lock is only held for the begin phase.\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param terminal The terminal handle to read from (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` or\n `terminal` is NULL, GHOSTTY_OUT_OF_MEMORY if updating the state requires\n allocation and that allocation fails\n\n @ingroup render"]
    pub fn ghostty_render_state_update(
        state: GhosttyRenderState,
        terminal: GhosttyTerminal,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Begin an update of a render state instance from a terminal.\n\n Every begin must be completed with a ghostty_render_state_end_update call\n before the render state is read.\n\n This two-phase structure exists for callers that synchronize access to the\n terminal state (e.g. with a lock shared with an IO thread): only this\n function requires terminal access, so a caller can hold its lock for this\n call only and then call ghostty_render_state_end_update after releasing\n it. The end phase exclusively reads and writes memory owned by the render\n state, so it is safe to call while the terminal is being modified.\n\n Work that doesn't require terminal access may be deferred to the end phase\n to keep this call (and therefore lock hold time) as short as possible.\n Callers must treat the render state as incomplete until\n ghostty_render_state_end_update is called.\n\n This consumes terminal/screen dirty state in the same way as the internal\n render state update path.\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param terminal The terminal handle to read from (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` or\n `terminal` is NULL, GHOSTTY_OUT_OF_MEMORY if updating the state requires\n allocation and that allocation fails\n\n @ingroup render"]
    pub fn ghostty_render_state_begin_update(
        state: GhosttyRenderState,
        terminal: GhosttyTerminal,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Complete a prior ghostty_render_state_begin_update call by performing any\n deferred work.\n\n This only reads and writes memory owned by the render state, so it is safe\n to call while the terminal is being modified (no terminal synchronization\n is required). Calling this without a prior begin is a safe no-op.\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` is\n NULL\n\n @ingroup render"]
    pub fn ghostty_render_state_end_update(state: GhosttyRenderState) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Mark all dirty render-state data as consumed.\n\n This sets the global dirty state to GHOSTTY_RENDER_STATE_DIRTY_FALSE and\n clears every per-row dirty flag. It is idempotent and does not modify cell\n contents or dirty state owned by the terminal. Call this only after a\n complete frame has been rendered successfully; partial consumers should\n use ghostty_render_state_set() and ghostty_render_state_row_set() instead.\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` is\n         NULL\n\n @ingroup render"]
    pub fn ghostty_render_state_clean(state: GhosttyRenderState) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get a value from a render state.\n\n The `out` pointer must point to a value of the type corresponding to the\n requested data kind (see GhosttyRenderStateData).\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param data The data kind to query\n @param[out] out Pointer to receive the queried value\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` or\n         `out` is NULL, `data` is not a recognized enum value, or a sized\n         output struct is smaller than `sizeof(size_t)`\n\n @ingroup render"]
    pub fn ghostty_render_state_get(
        state: GhosttyRenderState,
        data: GhosttyRenderStateData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from a render state in a single call.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup render"]
    pub fn ghostty_render_state_get_multi(
        state: GhosttyRenderState,
        count: usize,
        keys: *const GhosttyRenderStateData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Set an option on a render state.\n\n The `value` pointer must point to a value of the type corresponding to the\n requested option kind (see GhosttyRenderStateOption).\n\n @param state The render state handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param option The option to set\n @param[in] value Pointer to the value to set (NULL returns\n            GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `state` or\n         `value` is NULL\n\n @ingroup render"]
    pub fn ghostty_render_state_set(
        state: GhosttyRenderState,
        option: GhosttyRenderStateOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a new row iterator instance.\n\n All fields except the allocator are left undefined until populated\n via ghostty_render_state_get() with\n GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param[out] out_iterator On success, receives the created iterator handle\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n         failure\n\n @ingroup render"]
    pub fn ghostty_render_state_row_iterator_new(
        allocator: *const GhosttyAllocator,
        out_iterator: *mut GhosttyRenderStateRowIterator,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a render-state row iterator.\n\n @param iterator The iterator handle to free (may be NULL)\n\n @ingroup render"]
    pub fn ghostty_render_state_row_iterator_free(iterator: GhosttyRenderStateRowIterator);
}
unsafe extern "C" {
    #[doc = " Move a render-state row iterator to the next row.\n\n Rows are visited contiguously in ascending viewport order, starting at\n y = 0. Returns true if the iterator moved successfully and row data is\n available to read at the new position.\n\n @param iterator The iterator handle to advance (may be NULL)\n @return true if advanced to the next row, false if `iterator` is\n         NULL or if the iterator has reached the end\n\n @ingroup render"]
    pub fn ghostty_render_state_row_iterator_next(iterator: GhosttyRenderStateRowIterator) -> bool;
}
unsafe extern "C" {
    #[doc = " Move a render-state row iterator to the next row requiring a redraw.\n\n If the global dirty state is GHOSTTY_RENDER_STATE_DIRTY_FALSE, this returns\n false. If it is GHOSTTY_RENDER_STATE_DIRTY_PARTIAL, clean rows are skipped.\n If it is GHOSTTY_RENDER_STATE_DIRTY_FULL, every remaining row is returned\n regardless of its per-row dirty flag. Rows are returned in ascending\n viewport order. This function does not clear any dirty state.\n\n @param iterator The iterator handle to advance (NULL returns false)\n @param[out] out_y Receives the viewport y coordinate when true is returned\n                   (NULL returns false); it is not modified when false is\n                   returned\n @return true if advanced to a row requiring a redraw, false if an argument\n         is NULL or the iterator has reached the end of the effective dirty\n         rows\n\n @ingroup render"]
    pub fn ghostty_render_state_row_iterator_next_dirty(
        iterator: GhosttyRenderStateRowIterator,
        out_y: *mut u16,
    ) -> bool;
}
unsafe extern "C" {
    #[doc = " Get a value from the current row in a render-state row iterator.\n\n The `out` pointer must point to a value of the type corresponding to the\n requested data kind (see GhosttyRenderStateRowData).\n Call ghostty_render_state_row_iterator_next() or\n ghostty_render_state_row_iterator_next_dirty() at least once before\n calling this function.\n\n @param iterator The iterator handle to query (NULL returns GHOSTTY_INVALID_VALUE)\n @param data The data kind to query\n @param[out] out Pointer to receive the queried value\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if\n         `iterator` is NULL or the iterator is not positioned on a row\n\n @ingroup render"]
    pub fn ghostty_render_state_row_get(
        iterator: GhosttyRenderStateRowIterator,
        data: GhosttyRenderStateRowData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from the current row in a single call.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param iterator The iterator handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup render"]
    pub fn ghostty_render_state_row_get_multi(
        iterator: GhosttyRenderStateRowIterator,
        count: usize,
        keys: *const GhosttyRenderStateRowData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Set an option on the current row in a render-state row iterator.\n\n The `value` pointer must point to a value of the type corresponding to the\n requested option kind (see GhosttyRenderStateRowOption).\n Call ghostty_render_state_row_iterator_next() or\n ghostty_render_state_row_iterator_next_dirty() at least once before\n calling this function.\n\n @param iterator The iterator handle to update (NULL returns GHOSTTY_INVALID_VALUE)\n @param option The option to set\n @param[in] value Pointer to the value to set (NULL returns\n            GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if\n         `iterator` is NULL or the iterator is not positioned on a row\n\n @ingroup render"]
    pub fn ghostty_render_state_row_set(
        iterator: GhosttyRenderStateRowIterator,
        option: GhosttyRenderStateRowOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a new row cells instance.\n\n All fields except the allocator are left undefined until populated\n via ghostty_render_state_row_get() with\n GHOSTTY_RENDER_STATE_ROW_DATA_CELLS.\n\n You can reuse this value repeatedly with ghostty_render_state_row_get() to\n avoid allocating a new cells container for every row.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param[out] out_cells On success, receives the created row cells handle\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n         failure\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_new(
        allocator: *const GhosttyAllocator,
        out_cells: *mut GhosttyRenderStateRowCells,
    ) -> GhosttyResult;
}
#[doc = " Invalid / sentinel value."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_INVALID:
    GhosttyRenderStateRowCellsData = 0;
#[doc = " The raw cell value (GhosttyCell)."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW:
    GhosttyRenderStateRowCellsData = 1;
#[doc = " The style for the current cell (GhosttyStyle)."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE:
    GhosttyRenderStateRowCellsData = 2;
#[doc = " The total number of grapheme codepoints including the base codepoint\n  (uint32_t). Returns 0 if the cell has no text."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_LEN:
    GhosttyRenderStateRowCellsData = 3;
#[doc = " Write grapheme codepoints into a caller-provided buffer (uint32_t*).\n  The buffer must be at least graphemes_len elements. The base codepoint\n  is written first, followed by any extra codepoints."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_BUF:
    GhosttyRenderStateRowCellsData = 4;
#[doc = " The resolved background color of the cell (GhosttyColorRgb).\n  Flattens the three possible sources: content-tag bg_color_rgb,\n  content-tag bg_color_palette (looked up in the palette), or the\n  style's bg_color. Returns GHOSTTY_INVALID_VALUE if the cell has\n  no background color, in which case the caller should use whatever\n  default background color it wants (e.g. the terminal background)."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR:
    GhosttyRenderStateRowCellsData = 5;
#[doc = " The resolved foreground color of the cell (GhosttyColorRgb).\n  Resolves palette indices through the palette. Bold color handling\n  is not applied; the caller should handle bold styling separately.\n  Returns GHOSTTY_INVALID_VALUE if the cell has no explicit foreground\n  color, in which case the caller should use whatever default foreground\n  color it wants (e.g. the terminal foreground)."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR:
    GhosttyRenderStateRowCellsData = 6;
#[doc = " Whether the cell is contained within the current selection (bool).\n  This returns true when the cell's column is within the current row's\n  row-local selection range, and false otherwise. Rendering policy for\n  selected cells (colors, inversion, etc.) is left to the caller.\n\n  Renderers that can draw cells in spans may be more efficient querying\n  GHOSTTY_RENDER_STATE_ROW_DATA_SELECTION once per row and applying that\n  range directly, avoiding one C API call per cell for selection state."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_SELECTED:
    GhosttyRenderStateRowCellsData = 7;
#[doc = " Whether the cell has any explicit styling (bool).\n  This is equivalent to querying the raw cell's\n  GHOSTTY_CELL_DATA_HAS_STYLING value, but avoids materializing the raw\n  GhosttyCell for renderers that only need to know whether fetching the\n  full style is necessary."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_HAS_STYLING:
    GhosttyRenderStateRowCellsData = 8;
#[doc = " Encode the current cell's full grapheme cluster as UTF-8 into a\n caller-provided buffer (GhosttyBuffer).\n\n The base codepoint is encoded first, followed by any extra grapheme\n codepoints. Returns GHOSTTY_SUCCESS with len=0 when the cell has no text.\n\n If ptr is NULL or cap is too small for a non-empty cell, returns\n GHOSTTY_OUT_OF_SPACE without writing any bytes and sets len to the required\n buffer size in bytes."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8:
    GhosttyRenderStateRowCellsData = 9;
#[doc = " Encode the current cell's full grapheme cluster as UTF-8 into a\n caller-provided buffer (GhosttyBuffer).\n\n The base codepoint is encoded first, followed by any extra grapheme\n codepoints. Returns GHOSTTY_SUCCESS with len=0 when the cell has no text.\n\n If ptr is NULL or cap is too small for a non-empty cell, returns\n GHOSTTY_OUT_OF_SPACE without writing any bytes and sets len to the required\n buffer size in bytes."]
pub const GhosttyRenderStateRowCellsData_GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_MAX_VALUE:
    GhosttyRenderStateRowCellsData = 2147483647;
#[doc = " Queryable data kinds for ghostty_render_state_row_cells_get().\n\n @ingroup render"]
pub type GhosttyRenderStateRowCellsData = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Move a render-state row cells iterator to the next cell.\n\n Returns true if the iterator moved successfully and cell data is\n available to read at the new position.\n\n @param cells The row cells handle to advance (may be NULL)\n @return true if advanced to the next cell, false if `cells` is\n         NULL or if the iterator has reached the end\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_next(cells: GhosttyRenderStateRowCells) -> bool;
}
unsafe extern "C" {
    #[doc = " Move a render-state row cells iterator to a specific column.\n\n Positions the iterator at the given x (column) index so that\n subsequent reads return data for that cell.\n\n @param cells The row cells handle to reposition (NULL returns\n        GHOSTTY_INVALID_VALUE)\n @param x The zero-based column index to select\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if `cells`\n         is NULL or `x` is out of range\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_select(
        cells: GhosttyRenderStateRowCells,
        x: u16,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get a value from the current cell in a render-state row cells iterator.\n\n The `out` pointer must point to a value of the type corresponding to the\n requested data kind (see GhosttyRenderStateRowCellsData).\n Call ghostty_render_state_row_cells_next() or\n ghostty_render_state_row_cells_select() at least once before\n calling this function.\n\n @param cells The row cells handle to query (NULL returns GHOSTTY_INVALID_VALUE)\n @param data The data kind to query\n @param[out] out Pointer to receive the queried value\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if\n         `cells` is NULL or the iterator is not positioned on a cell\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_get(
        cells: GhosttyRenderStateRowCells,
        data: GhosttyRenderStateRowCellsData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from the current cell in a single call.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param cells The row cells handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_get_multi(
        cells: GhosttyRenderStateRowCells,
        count: usize,
        keys: *const GhosttyRenderStateRowCellsData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a row cells instance.\n\n @param cells The row cells handle to free (may be NULL)\n\n @ingroup render"]
    pub fn ghostty_render_state_row_cells_free(cells: GhosttyRenderStateRowCells);
}
unsafe extern "C" {
    #[doc = " Free a tracked grid reference.\n\n Passing NULL is allowed and has no effect. A tracked reference may be freed\n after the terminal that created it is freed.\n\n @param ref Tracked grid reference to free.\n\n @ingroup grid_ref"]
    pub fn ghostty_tracked_grid_ref_free(ref_: GhosttyTrackedGridRef);
}
unsafe extern "C" {
    #[doc = " Return whether a tracked grid reference currently has a meaningful value.\n\n If the terminal that created the tracked reference has been freed, this\n returns false.\n\n @param ref Tracked grid reference.\n @return true if the reference currently has a meaningful value.\n\n @ingroup grid_ref"]
    pub fn ghostty_tracked_grid_ref_has_value(ref_: GhosttyTrackedGridRef) -> bool;
}
unsafe extern "C" {
    #[doc = " Convert a tracked grid reference to a point in the requested coordinate\n space.\n\n This is the tracked equivalent of ghostty_terminal_point_from_grid_ref().\n Unlike snapshotting, this does not expose an intermediate untracked\n GhosttyGridRef.\n\n A tracked reference is resolved against the terminal screen/page-list that\n currently owns the reference. If the terminal has switched between primary\n and alternate screens since the reference was created or last set, this may\n be different from the terminal's currently active screen.\n\n If the tracked reference no longer has a meaningful value, this returns\n GHOSTTY_NO_VALUE. GHOSTTY_NO_VALUE is also returned when the reference cannot\n be represented in the requested coordinate space, including after the\n terminal that created the tracked reference has been freed.\n\n @param ref Tracked grid reference.\n @param tag Coordinate space to convert into.\n @param[out] out_point On success, receives the coordinate. May be NULL.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if ref is invalid,\n         or GHOSTTY_NO_VALUE if there is no representable value.\n\n @ingroup grid_ref"]
    pub fn ghostty_tracked_grid_ref_point(
        ref_: GhosttyTrackedGridRef,
        tag: GhosttyPointTag,
        out_point: *mut GhosttyPointCoordinate,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Move an existing tracked grid reference to a new terminal point.\n\n On success, the tracked reference begins tracking the new point and any prior\n \"no value\" state is cleared. On GHOSTTY_OUT_OF_MEMORY, the original tracked\n reference is left unchanged.\n\n The terminal must be the same terminal that created the tracked reference.\n The point is resolved against the terminal screen/page-list that is active at\n the time this function is called. If the terminal has switched between\n primary and alternate screens, this may move the tracked reference from one\n screen/page-list to the other.\n\n @param ref Tracked grid reference.\n @param terminal Terminal instance that owns the reference.\n @param point New point to track.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if ref, terminal,\n         or point is invalid, or GHOSTTY_OUT_OF_MEMORY if allocation fails.\n\n @ingroup grid_ref"]
    pub fn ghostty_tracked_grid_ref_set(
        ref_: GhosttyTrackedGridRef,
        terminal: GhosttyTerminal,
        point: GhosttyPoint,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Snapshot a tracked grid reference into a regular GhosttyGridRef.\n\n The returned GhosttyGridRef is an untracked snapshot and has the same\n lifetime rules as ghostty_terminal_grid_ref(): it is only valid until the\n next terminal update. Snapshot immediately before calling\n ghostty_grid_ref_cell(), ghostty_grid_ref_row(),\n ghostty_grid_ref_graphemes(), ghostty_grid_ref_hyperlink_uri(), or\n ghostty_grid_ref_style().\n\n If the tracked reference no longer has a meaningful value, this returns\n GHOSTTY_NO_VALUE. This includes references whose owning terminal has been\n freed.\n\n @param ref Tracked grid reference.\n @param[out] out_ref On success, receives an untracked snapshot. May be NULL.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if ref is invalid,\n         or GHOSTTY_NO_VALUE if the tracked location was discarded.\n\n @ingroup grid_ref"]
    pub fn ghostty_tracked_grid_ref_snapshot(
        ref_: GhosttyTrackedGridRef,
        out_ref: *mut GhosttyGridRef,
    ) -> GhosttyResult;
}
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_INVALID: GhosttyOscCommandType = 0;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CHANGE_WINDOW_TITLE: GhosttyOscCommandType = 1;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CHANGE_WINDOW_ICON: GhosttyOscCommandType = 2;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_SEMANTIC_PROMPT: GhosttyOscCommandType = 3;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CLIPBOARD_CONTENTS: GhosttyOscCommandType = 4;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_REPORT_PWD: GhosttyOscCommandType = 5;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_MOUSE_SHAPE: GhosttyOscCommandType = 6;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_COLOR_OPERATION: GhosttyOscCommandType = 7;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_KITTY_COLOR_PROTOCOL: GhosttyOscCommandType = 8;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_SHOW_DESKTOP_NOTIFICATION:
    GhosttyOscCommandType = 9;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_HYPERLINK_START: GhosttyOscCommandType = 10;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_HYPERLINK_END: GhosttyOscCommandType = 11;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_SLEEP: GhosttyOscCommandType = 12;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_SHOW_MESSAGE_BOX: GhosttyOscCommandType =
    13;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_CHANGE_TAB_TITLE: GhosttyOscCommandType =
    14;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_PROGRESS_REPORT: GhosttyOscCommandType =
    15;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_WAIT_INPUT: GhosttyOscCommandType = 16;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_GUIMACRO: GhosttyOscCommandType = 17;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_RUN_PROCESS: GhosttyOscCommandType = 18;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_OUTPUT_ENVIRONMENT_VARIABLE:
    GhosttyOscCommandType = 19;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_XTERM_EMULATION: GhosttyOscCommandType =
    20;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONEMU_COMMENT: GhosttyOscCommandType = 21;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_KITTY_TEXT_SIZING: GhosttyOscCommandType = 22;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_KITTY_CLIPBOARD_PROTOCOL:
    GhosttyOscCommandType = 23;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_KITTY_DND_PROTOCOL: GhosttyOscCommandType = 24;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_CONTEXT_SIGNAL: GhosttyOscCommandType = 25;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_KITTY_DESKTOP_NOTIFICATION:
    GhosttyOscCommandType = 26;
pub const GhosttyOscCommandType_GHOSTTY_OSC_COMMAND_TYPE_MAX_VALUE: GhosttyOscCommandType =
    2147483647;
#[doc = " OSC command types.\n\n @ingroup osc"]
pub type GhosttyOscCommandType = ::std::os::raw::c_int;
#[doc = " Invalid data type. Never results in any data extraction."]
pub const GhosttyOscCommandData_GHOSTTY_OSC_DATA_INVALID: GhosttyOscCommandData = 0;
#[doc = " Window title string data.\n\n Valid for: GHOSTTY_OSC_COMMAND_CHANGE_WINDOW_TITLE\n\n Output type: const char ** (pointer to null-terminated string)\n\n Lifetime: Valid until the next call to any ghostty_osc_* function with\n the same parser instance. Memory is owned by the parser."]
pub const GhosttyOscCommandData_GHOSTTY_OSC_DATA_CHANGE_WINDOW_TITLE_STR: GhosttyOscCommandData = 1;
#[doc = " Window title string data.\n\n Valid for: GHOSTTY_OSC_COMMAND_CHANGE_WINDOW_TITLE\n\n Output type: const char ** (pointer to null-terminated string)\n\n Lifetime: Valid until the next call to any ghostty_osc_* function with\n the same parser instance. Memory is owned by the parser."]
pub const GhosttyOscCommandData_GHOSTTY_OSC_DATA_MAX_VALUE: GhosttyOscCommandData = 2147483647;
#[doc = " OSC command data types.\n\n These values specify what type of data to extract from an OSC command\n using `ghostty_osc_command_data`.\n\n @ingroup osc"]
pub type GhosttyOscCommandData = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Create a new OSC parser instance.\n\n Creates a new OSC (Operating System Command) parser using the provided\n allocator. The parser must be freed using ghostty_vt_osc_free() when\n no longer needed.\n\n @param allocator Pointer to the allocator to use for memory management, or NULL to use the default allocator\n @param parser Pointer to store the created parser handle\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup osc"]
    pub fn ghostty_osc_new(
        allocator: *const GhosttyAllocator,
        parser: *mut GhosttyOscParser,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free an OSC parser instance.\n\n Releases all resources associated with the OSC parser. After this call,\n the parser handle becomes invalid and must not be used.\n\n @param parser The parser handle to free (may be NULL)\n\n @ingroup osc"]
    pub fn ghostty_osc_free(parser: GhosttyOscParser);
}
unsafe extern "C" {
    #[doc = " Reset an OSC parser instance to its initial state.\n\n Resets the parser state, clearing any partially parsed OSC sequences\n and returning the parser to its initial state. This is useful for\n reusing a parser instance or recovering from parse errors.\n\n @param parser The parser handle to reset, must not be null.\n\n @ingroup osc"]
    pub fn ghostty_osc_reset(parser: GhosttyOscParser);
}
unsafe extern "C" {
    #[doc = " Parse the next byte in an OSC sequence.\n\n Processes a single byte as part of an OSC sequence. The parser maintains\n internal state to track the progress through the sequence. Call this\n function for each byte in the sequence data.\n\n When finished pumping the parser with bytes, call ghostty_osc_end\n to get the final result.\n\n @param parser The parser handle, must not be null.\n @param byte The next byte to parse\n\n @ingroup osc"]
    pub fn ghostty_osc_next(parser: GhosttyOscParser, byte: u8);
}
unsafe extern "C" {
    #[doc = " Finalize OSC parsing and retrieve the parsed command.\n\n Call this function after feeding all bytes of an OSC sequence to the parser\n using ghostty_osc_next() with the exception of the terminating character\n (ESC or ST). This function finalizes the parsing process and returns the\n parsed OSC command.\n\n The return value is never NULL. Invalid commands will return a command\n with type GHOSTTY_OSC_COMMAND_INVALID.\n\n The terminator parameter specifies the byte that terminated the OSC sequence\n (typically 0x07 for BEL or 0x5C for ST after ESC). This information is\n preserved in the parsed command so that responses can use the same terminator\n format for better compatibility with the calling program. For commands that\n do not require a response, this parameter is ignored and the resulting\n command will not retain the terminator information.\n\n The returned command handle is valid until the next call to any\n `ghostty_osc_*` function with the same parser instance with the exception\n of command introspection functions such as `ghostty_osc_command_type`.\n\n @param parser The parser handle, must not be null.\n @param terminator The terminating byte of the OSC sequence (0x07 for BEL, 0x5C for ST)\n @return Handle to the parsed OSC command\n\n @ingroup osc"]
    pub fn ghostty_osc_end(parser: GhosttyOscParser, terminator: u8) -> GhosttyOscCommand;
}
unsafe extern "C" {
    #[doc = " Get the type of an OSC command.\n\n Returns the type identifier for the given OSC command. This can be used\n to determine what kind of command was parsed and what data might be\n available from it.\n\n @param command The OSC command handle to query (may be NULL)\n @return The command type, or GHOSTTY_OSC_COMMAND_INVALID if command is NULL\n\n @ingroup osc"]
    pub fn ghostty_osc_command_type(command: GhosttyOscCommand) -> GhosttyOscCommandType;
}
unsafe extern "C" {
    #[doc = " Extract data from an OSC command.\n\n Extracts typed data from the given OSC command based on the specified\n data type. The output pointer must be of the appropriate type for the\n requested data kind. Valid command types, output types, and memory\n safety information are documented in the `GhosttyOscCommandData` enum.\n\n @param command The OSC command handle to query (may be NULL)\n @param data The type of data to extract\n @param out Pointer to store the extracted data (type depends on data parameter)\n @return true if data extraction was successful, false otherwise\n\n @ingroup osc"]
    pub fn ghostty_osc_command_data(
        command: GhosttyOscCommand,
        data: GhosttyOscCommandData,
        out: *mut ::std::os::raw::c_void,
    ) -> bool;
}
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_UNSET: GhosttySgrAttributeTag = 0;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_UNKNOWN: GhosttySgrAttributeTag = 1;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BOLD: GhosttySgrAttributeTag = 2;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_BOLD: GhosttySgrAttributeTag = 3;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_ITALIC: GhosttySgrAttributeTag = 4;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_ITALIC: GhosttySgrAttributeTag = 5;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_FAINT: GhosttySgrAttributeTag = 6;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_UNDERLINE: GhosttySgrAttributeTag = 7;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_UNDERLINE_COLOR: GhosttySgrAttributeTag = 8;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_UNDERLINE_COLOR_256: GhosttySgrAttributeTag = 9;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_UNDERLINE_COLOR: GhosttySgrAttributeTag =
    10;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_OVERLINE: GhosttySgrAttributeTag = 11;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_OVERLINE: GhosttySgrAttributeTag = 12;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BLINK: GhosttySgrAttributeTag = 13;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_BLINK: GhosttySgrAttributeTag = 14;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_INVERSE: GhosttySgrAttributeTag = 15;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_INVERSE: GhosttySgrAttributeTag = 16;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_INVISIBLE: GhosttySgrAttributeTag = 17;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_INVISIBLE: GhosttySgrAttributeTag = 18;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_STRIKETHROUGH: GhosttySgrAttributeTag = 19;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_STRIKETHROUGH: GhosttySgrAttributeTag = 20;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_DIRECT_COLOR_FG: GhosttySgrAttributeTag = 21;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_DIRECT_COLOR_BG: GhosttySgrAttributeTag = 22;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BG_8: GhosttySgrAttributeTag = 23;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_FG_8: GhosttySgrAttributeTag = 24;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_FG: GhosttySgrAttributeTag = 25;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_RESET_BG: GhosttySgrAttributeTag = 26;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BRIGHT_BG_8: GhosttySgrAttributeTag = 27;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BRIGHT_FG_8: GhosttySgrAttributeTag = 28;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_BG_256: GhosttySgrAttributeTag = 29;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_FG_256: GhosttySgrAttributeTag = 30;
pub const GhosttySgrAttributeTag_GHOSTTY_SGR_ATTR_MAX_VALUE: GhosttySgrAttributeTag = 2147483647;
#[doc = " SGR attribute tags.\n\n These values identify the type of an SGR attribute in a tagged union.\n Use the tag to determine which field in the attribute value union to access.\n\n @ingroup sgr"]
pub type GhosttySgrAttributeTag = ::std::os::raw::c_int;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_NONE: GhosttySgrUnderline = 0;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_SINGLE: GhosttySgrUnderline = 1;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_DOUBLE: GhosttySgrUnderline = 2;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_CURLY: GhosttySgrUnderline = 3;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_DOTTED: GhosttySgrUnderline = 4;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_DASHED: GhosttySgrUnderline = 5;
pub const GhosttySgrUnderline_GHOSTTY_SGR_UNDERLINE_MAX_VALUE: GhosttySgrUnderline = 2147483647;
#[doc = " Underline style types.\n\n @ingroup sgr"]
pub type GhosttySgrUnderline = ::std::os::raw::c_int;
#[doc = " Unknown SGR attribute data.\n\n Contains the full parameter list and the partial list where parsing\n encountered an unknown or invalid sequence.\n\n @ingroup sgr"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySgrUnknown {
    pub full_ptr: *const u16,
    pub full_len: usize,
    pub partial_ptr: *const u16,
    pub partial_len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySgrUnknown"][::std::mem::size_of::<GhosttySgrUnknown>() - 32usize];
    ["Alignment of GhosttySgrUnknown"][::std::mem::align_of::<GhosttySgrUnknown>() - 8usize];
    ["Offset of field: GhosttySgrUnknown::full_ptr"]
        [::std::mem::offset_of!(GhosttySgrUnknown, full_ptr) - 0usize];
    ["Offset of field: GhosttySgrUnknown::full_len"]
        [::std::mem::offset_of!(GhosttySgrUnknown, full_len) - 8usize];
    ["Offset of field: GhosttySgrUnknown::partial_ptr"]
        [::std::mem::offset_of!(GhosttySgrUnknown, partial_ptr) - 16usize];
    ["Offset of field: GhosttySgrUnknown::partial_len"]
        [::std::mem::offset_of!(GhosttySgrUnknown, partial_len) - 24usize];
};
impl Default for GhosttySgrUnknown {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " SGR attribute value union.\n\n This union contains all possible attribute values. Use the tag field\n to determine which union member is active. Attributes without associated\n data (like bold, italic) don't use the union value.\n\n @ingroup sgr"]
#[repr(C)]
#[derive(Copy, Clone)]
pub union GhosttySgrAttributeValue {
    pub unknown: GhosttySgrUnknown,
    pub underline: GhosttySgrUnderline,
    pub underline_color: GhosttyColorRgb,
    pub underline_color_256: GhosttyColorPaletteIndex,
    pub direct_color_fg: GhosttyColorRgb,
    pub direct_color_bg: GhosttyColorRgb,
    pub bg_8: GhosttyColorPaletteIndex,
    pub fg_8: GhosttyColorPaletteIndex,
    pub bright_bg_8: GhosttyColorPaletteIndex,
    pub bright_fg_8: GhosttyColorPaletteIndex,
    pub bg_256: GhosttyColorPaletteIndex,
    pub fg_256: GhosttyColorPaletteIndex,
    pub _padding: [u64; 8usize],
}
