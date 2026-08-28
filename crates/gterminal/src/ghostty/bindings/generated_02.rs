impl Default for GhosttyStyleColor {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Terminal cell style.\n\n Describes the complete visual style for a terminal cell, including\n foreground, background, and underline colors, as well as text\n decoration flags. The underline field uses the same values as\n GhosttySgrUnderline.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n\n @ingroup style"]
#[repr(C)]
#[derive(Copy, Clone)]
pub struct GhosttyStyle {
    pub size: usize,
    pub fg_color: GhosttyStyleColor,
    pub bg_color: GhosttyStyleColor,
    pub underline_color: GhosttyStyleColor,
    pub bold: bool,
    pub italic: bool,
    pub faint: bool,
    pub blink: bool,
    pub inverse: bool,
    pub invisible: bool,
    pub strikethrough: bool,
    pub overline: bool,
    #[doc = "< One of GHOSTTY_SGR_UNDERLINE_* values"]
    pub underline: ::std::os::raw::c_int,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyStyle"][::std::mem::size_of::<GhosttyStyle>() - 72usize];
    ["Alignment of GhosttyStyle"][::std::mem::align_of::<GhosttyStyle>() - 8usize];
    ["Offset of field: GhosttyStyle::size"][::std::mem::offset_of!(GhosttyStyle, size) - 0usize];
    ["Offset of field: GhosttyStyle::fg_color"]
        [::std::mem::offset_of!(GhosttyStyle, fg_color) - 8usize];
    ["Offset of field: GhosttyStyle::bg_color"]
        [::std::mem::offset_of!(GhosttyStyle, bg_color) - 24usize];
    ["Offset of field: GhosttyStyle::underline_color"]
        [::std::mem::offset_of!(GhosttyStyle, underline_color) - 40usize];
    ["Offset of field: GhosttyStyle::bold"][::std::mem::offset_of!(GhosttyStyle, bold) - 56usize];
    ["Offset of field: GhosttyStyle::italic"]
        [::std::mem::offset_of!(GhosttyStyle, italic) - 57usize];
    ["Offset of field: GhosttyStyle::faint"][::std::mem::offset_of!(GhosttyStyle, faint) - 58usize];
    ["Offset of field: GhosttyStyle::blink"][::std::mem::offset_of!(GhosttyStyle, blink) - 59usize];
    ["Offset of field: GhosttyStyle::inverse"]
        [::std::mem::offset_of!(GhosttyStyle, inverse) - 60usize];
    ["Offset of field: GhosttyStyle::invisible"]
        [::std::mem::offset_of!(GhosttyStyle, invisible) - 61usize];
    ["Offset of field: GhosttyStyle::strikethrough"]
        [::std::mem::offset_of!(GhosttyStyle, strikethrough) - 62usize];
    ["Offset of field: GhosttyStyle::overline"]
        [::std::mem::offset_of!(GhosttyStyle, overline) - 63usize];
    ["Offset of field: GhosttyStyle::underline"]
        [::std::mem::offset_of!(GhosttyStyle, underline) - 64usize];
};
impl Default for GhosttyStyle {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
unsafe extern "C" {
    #[doc = " Get the default style.\n\n Initializes the style to the default values (no colors, no flags).\n\n @param style Pointer to the style to initialize\n\n @ingroup style"]
    pub fn ghostty_style_default(style: *mut GhosttyStyle);
}
unsafe extern "C" {
    #[doc = " Check if a style is the default style.\n\n Returns true if all colors are unset and all flags are off.\n\n @param style Pointer to the style to check\n @return true if the style is the default style\n\n @ingroup style"]
    pub fn ghostty_style_is_default(style: *const GhosttyStyle) -> bool;
}
#[doc = " A resolved reference to a terminal cell position.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n\n @ingroup grid_ref"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyGridRef {
    pub size: usize,
    pub node: *mut ::std::os::raw::c_void,
    pub x: u16,
    pub y: u16,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyGridRef"][::std::mem::size_of::<GhosttyGridRef>() - 24usize];
    ["Alignment of GhosttyGridRef"][::std::mem::align_of::<GhosttyGridRef>() - 8usize];
    ["Offset of field: GhosttyGridRef::size"]
        [::std::mem::offset_of!(GhosttyGridRef, size) - 0usize];
    ["Offset of field: GhosttyGridRef::node"]
        [::std::mem::offset_of!(GhosttyGridRef, node) - 8usize];
    ["Offset of field: GhosttyGridRef::x"][::std::mem::offset_of!(GhosttyGridRef, x) - 16usize];
    ["Offset of field: GhosttyGridRef::y"][::std::mem::offset_of!(GhosttyGridRef, y) - 18usize];
};
impl Default for GhosttyGridRef {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
unsafe extern "C" {
    #[doc = " Get the cell from a grid reference.\n\n @param ref Pointer to the grid reference\n @param[out] out_cell On success, set to the cell at the ref's position (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the ref's\n         node is NULL\n\n @ingroup grid_ref"]
    pub fn ghostty_grid_ref_cell(
        ref_: *const GhosttyGridRef,
        out_cell: *mut GhosttyCell,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the row from a grid reference.\n\n @param ref Pointer to the grid reference\n @param[out] out_row On success, set to the row at the ref's position (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the ref's\n         node is NULL\n\n @ingroup grid_ref"]
    pub fn ghostty_grid_ref_row(
        ref_: *const GhosttyGridRef,
        out_row: *mut GhosttyRow,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the grapheme cluster codepoints for the cell at the grid reference's\n position.\n\n Writes the full grapheme cluster (the cell's primary codepoint followed by\n any combining codepoints) into the provided buffer. If the cell has no text,\n out_len is set to 0 and GHOSTTY_SUCCESS is returned.\n\n If the buffer is too small (or NULL), the function returns\n GHOSTTY_OUT_OF_SPACE and writes the required number of codepoints to\n out_len. The caller can then retry with a sufficiently sized buffer.\n\n @param ref Pointer to the grid reference\n @param buf Output buffer of uint32_t codepoints (may be NULL)\n @param buf_len Number of uint32_t elements in the buffer\n @param[out] out_len On success, the number of codepoints written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size in codepoints.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the ref's\n         node is NULL, GHOSTTY_OUT_OF_SPACE if the buffer is too small\n\n @ingroup grid_ref"]
    pub fn ghostty_grid_ref_graphemes(
        ref_: *const GhosttyGridRef,
        buf: *mut u32,
        buf_len: usize,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the hyperlink URI for the cell at the grid reference's position.\n\n Writes the URI bytes into the provided buffer. If the cell has no\n hyperlink, out_len is set to 0 and GHOSTTY_SUCCESS is returned.\n\n If the buffer is too small (or NULL), the function returns\n GHOSTTY_OUT_OF_SPACE and writes the required number of bytes to\n out_len. The caller can then retry with a sufficiently sized buffer.\n\n @param ref Pointer to the grid reference\n @param buf Output buffer for the URI bytes (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_len On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size in bytes.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the ref's\n         node is NULL, GHOSTTY_OUT_OF_SPACE if the buffer is too small\n\n @ingroup grid_ref"]
    pub fn ghostty_grid_ref_hyperlink_uri(
        ref_: *const GhosttyGridRef,
        buf: *mut u8,
        buf_len: usize,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the style of the cell at the grid reference's position.\n\n @param ref Pointer to the grid reference\n @param[out] out_style On success, set to the cell's style (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the ref's\n         node is NULL\n\n @ingroup grid_ref"]
    pub fn ghostty_grid_ref_style(
        ref_: *const GhosttyGridRef,
        out_style: *mut GhosttyStyle,
    ) -> GhosttyResult;
}
#[doc = " A coordinate in the terminal grid.\n\n @ingroup point"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyPointCoordinate {
    #[doc = " Column (0-indexed)."]
    pub x: u16,
    #[doc = " Row (0-indexed). May exceed page size for screen/history tags."]
    pub y: u32,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyPointCoordinate"][::std::mem::size_of::<GhosttyPointCoordinate>() - 8usize];
    ["Alignment of GhosttyPointCoordinate"]
        [::std::mem::align_of::<GhosttyPointCoordinate>() - 4usize];
    ["Offset of field: GhosttyPointCoordinate::x"]
        [::std::mem::offset_of!(GhosttyPointCoordinate, x) - 0usize];
    ["Offset of field: GhosttyPointCoordinate::y"]
        [::std::mem::offset_of!(GhosttyPointCoordinate, y) - 4usize];
};
#[doc = " Active area where the cursor can move."]
pub const GhosttyPointTag_GHOSTTY_POINT_TAG_ACTIVE: GhosttyPointTag = 0;
#[doc = " Visible viewport (changes when scrolled)."]
pub const GhosttyPointTag_GHOSTTY_POINT_TAG_VIEWPORT: GhosttyPointTag = 1;
#[doc = " Full screen including scrollback."]
pub const GhosttyPointTag_GHOSTTY_POINT_TAG_SCREEN: GhosttyPointTag = 2;
#[doc = " Scrollback history only (before active area)."]
pub const GhosttyPointTag_GHOSTTY_POINT_TAG_HISTORY: GhosttyPointTag = 3;
#[doc = " Scrollback history only (before active area)."]
pub const GhosttyPointTag_GHOSTTY_POINT_TAG_MAX_VALUE: GhosttyPointTag = 2147483647;
#[doc = " Point reference tag.\n\n Determines which coordinate system a point uses.\n\n @ingroup point"]
pub type GhosttyPointTag = ::std::os::raw::c_uint;
#[doc = " Point value union.\n\n @ingroup point"]
#[repr(C)]
#[derive(Copy, Clone)]
pub union GhosttyPointValue {
    #[doc = " Coordinate (used for all tag variants)."]
    pub coordinate: GhosttyPointCoordinate,
    #[doc = " Padding for ABI compatibility. Do not use."]
    pub _padding: [u64; 2usize],
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyPointValue"][::std::mem::size_of::<GhosttyPointValue>() - 16usize];
    ["Alignment of GhosttyPointValue"][::std::mem::align_of::<GhosttyPointValue>() - 8usize];
    ["Offset of field: GhosttyPointValue::coordinate"]
        [::std::mem::offset_of!(GhosttyPointValue, coordinate) - 0usize];
    ["Offset of field: GhosttyPointValue::_padding"]
        [::std::mem::offset_of!(GhosttyPointValue, _padding) - 0usize];
};
impl Default for GhosttyPointValue {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Tagged union for a point in the terminal grid.\n\n @ingroup point"]
#[repr(C)]
#[derive(Copy, Clone)]
pub struct GhosttyPoint {
    pub tag: GhosttyPointTag,
    pub value: GhosttyPointValue,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyPoint"][::std::mem::size_of::<GhosttyPoint>() - 24usize];
    ["Alignment of GhosttyPoint"][::std::mem::align_of::<GhosttyPoint>() - 8usize];
    ["Offset of field: GhosttyPoint::tag"][::std::mem::offset_of!(GhosttyPoint, tag) - 0usize];
    ["Offset of field: GhosttyPoint::value"][::std::mem::offset_of!(GhosttyPoint, value) - 8usize];
};
impl Default for GhosttyPoint {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySelectionGestureImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to state for interpreting terminal selection gestures.\n\n The gesture owns only the state required to interpret pointer events. Calls\n that use a gesture are not concurrency-safe and must be serialized with\n terminal mutations.\n\n @ingroup selection"]
pub type GhosttySelectionGesture = *mut GhosttySelectionGestureImpl;
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySelectionGestureEventImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to reusable input data for selection gesture operations.\n\n Event options are set with ghostty_selection_gesture_event_set(). Individual\n gesture operations document which options are required or optional.\n\n @ingroup selection"]
pub type GhosttySelectionGestureEvent = *mut GhosttySelectionGestureEventImpl;
#[doc = " A snapshot selection range defined by two grid references.\n\n Both endpoints are inclusive. The endpoints preserve selection direction\n and may be reversed; callers must not assume that start is the top-left\n endpoint or that end is the bottom-right endpoint.\n\n When rectangle is false, the endpoints describe a linear selection. When\n rectangle is true, the same endpoints are interpreted as opposite corners\n of a rectangular/block selection.\n\n The start and end values are untracked GhosttyGridRef snapshots and are\n only valid until the next mutating operation on the terminal that produced\n them unless the selection is reconstructed from tracked references.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySelection {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttySelection)."]
    pub size: usize,
    #[doc = " Start of the selection range (inclusive).\n\n This may be after end in terminal order. It is an untracked\n GhosttyGridRef snapshot and follows untracked grid-ref lifetime rules."]
    pub start: GhosttyGridRef,
    #[doc = " End of the selection range (inclusive).\n\n This may be before start in terminal order. It is an untracked\n GhosttyGridRef snapshot and follows untracked grid-ref lifetime rules."]
    pub end: GhosttyGridRef,
    #[doc = " Whether the endpoints are interpreted as a rectangular/block selection\n rather than a linear selection."]
    pub rectangle: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySelection"][::std::mem::size_of::<GhosttySelection>() - 64usize];
    ["Alignment of GhosttySelection"][::std::mem::align_of::<GhosttySelection>() - 8usize];
    ["Offset of field: GhosttySelection::size"]
        [::std::mem::offset_of!(GhosttySelection, size) - 0usize];
    ["Offset of field: GhosttySelection::start"]
        [::std::mem::offset_of!(GhosttySelection, start) - 8usize];
    ["Offset of field: GhosttySelection::end"]
        [::std::mem::offset_of!(GhosttySelection, end) - 32usize];
    ["Offset of field: GhosttySelection::rectangle"]
        [::std::mem::offset_of!(GhosttySelection, rectangle) - 56usize];
};
impl Default for GhosttySelection {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Options for deriving a word selection from a terminal grid reference.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n If boundary_codepoints is NULL and boundary_codepoints_len is 0, Ghostty's\n default word-boundary codepoints are used. If boundary_codepoints_len is\n non-zero, boundary_codepoints must not be NULL.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalSelectWordOptions {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyTerminalSelectWordOptions)."]
    pub size: usize,
    #[doc = " Grid reference under which to derive the word selection."]
    pub ref_: GhosttyGridRef,
    #[doc = " Optional word-boundary codepoints as uint32_t scalar values."]
    pub boundary_codepoints: *const u32,
    #[doc = " Number of entries in boundary_codepoints."]
    pub boundary_codepoints_len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalSelectWordOptions"]
        [::std::mem::size_of::<GhosttyTerminalSelectWordOptions>() - 48usize];
    ["Alignment of GhosttyTerminalSelectWordOptions"]
        [::std::mem::align_of::<GhosttyTerminalSelectWordOptions>() - 8usize];
    ["Offset of field: GhosttyTerminalSelectWordOptions::size"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordOptions, size) - 0usize];
    ["Offset of field: GhosttyTerminalSelectWordOptions::ref_"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordOptions, ref_) - 8usize];
    ["Offset of field: GhosttyTerminalSelectWordOptions::boundary_codepoints"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordOptions, boundary_codepoints) - 32usize];
    ["Offset of field: GhosttyTerminalSelectWordOptions::boundary_codepoints_len"][::std::mem::offset_of!(
        GhosttyTerminalSelectWordOptions,
        boundary_codepoints_len
    ) - 40usize];
};
impl Default for GhosttyTerminalSelectWordOptions {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Options for deriving the nearest word selection between two grid references.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n If boundary_codepoints is NULL and boundary_codepoints_len is 0, Ghostty's\n default word-boundary codepoints are used. If boundary_codepoints_len is\n non-zero, boundary_codepoints must not be NULL.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalSelectWordBetweenOptions {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyTerminalSelectWordBetweenOptions)."]
    pub size: usize,
    #[doc = " Starting grid reference for the inclusive search range."]
    pub start: GhosttyGridRef,
    #[doc = " Ending grid reference for the inclusive search range."]
    pub end: GhosttyGridRef,
    #[doc = " Optional word-boundary codepoints as uint32_t scalar values."]
    pub boundary_codepoints: *const u32,
    #[doc = " Number of entries in boundary_codepoints."]
    pub boundary_codepoints_len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalSelectWordBetweenOptions"]
        [::std::mem::size_of::<GhosttyTerminalSelectWordBetweenOptions>() - 72usize];
    ["Alignment of GhosttyTerminalSelectWordBetweenOptions"]
        [::std::mem::align_of::<GhosttyTerminalSelectWordBetweenOptions>() - 8usize];
    ["Offset of field: GhosttyTerminalSelectWordBetweenOptions::size"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordBetweenOptions, size) - 0usize];
    ["Offset of field: GhosttyTerminalSelectWordBetweenOptions::start"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordBetweenOptions, start) - 8usize];
    ["Offset of field: GhosttyTerminalSelectWordBetweenOptions::end"]
        [::std::mem::offset_of!(GhosttyTerminalSelectWordBetweenOptions, end) - 32usize];
    ["Offset of field: GhosttyTerminalSelectWordBetweenOptions::boundary_codepoints"][::std::mem::offset_of!(
        GhosttyTerminalSelectWordBetweenOptions,
        boundary_codepoints
    ) - 56usize];
    ["Offset of field: GhosttyTerminalSelectWordBetweenOptions::boundary_codepoints_len"][::std::mem::offset_of!(
        GhosttyTerminalSelectWordBetweenOptions,
        boundary_codepoints_len
    )
        - 64usize];
};
impl Default for GhosttyTerminalSelectWordBetweenOptions {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Options for deriving a line selection from a terminal grid reference.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n If whitespace is NULL and whitespace_len is 0, Ghostty's default line-trim\n whitespace codepoints are used. If whitespace_len is non-zero, whitespace\n must not be NULL.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalSelectLineOptions {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyTerminalSelectLineOptions)."]
    pub size: usize,
    #[doc = " Grid reference under which to derive the line selection."]
    pub ref_: GhosttyGridRef,
    #[doc = " Optional codepoints to trim from the start and end of the line."]
    pub whitespace: *const u32,
    #[doc = " Number of entries in whitespace."]
    pub whitespace_len: usize,
    #[doc = " Whether semantic prompt state changes should bound the line selection."]
    pub semantic_prompt_boundary: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalSelectLineOptions"]
        [::std::mem::size_of::<GhosttyTerminalSelectLineOptions>() - 56usize];
    ["Alignment of GhosttyTerminalSelectLineOptions"]
        [::std::mem::align_of::<GhosttyTerminalSelectLineOptions>() - 8usize];
    ["Offset of field: GhosttyTerminalSelectLineOptions::size"]
        [::std::mem::offset_of!(GhosttyTerminalSelectLineOptions, size) - 0usize];
    ["Offset of field: GhosttyTerminalSelectLineOptions::ref_"]
        [::std::mem::offset_of!(GhosttyTerminalSelectLineOptions, ref_) - 8usize];
    ["Offset of field: GhosttyTerminalSelectLineOptions::whitespace"]
        [::std::mem::offset_of!(GhosttyTerminalSelectLineOptions, whitespace) - 32usize];
    ["Offset of field: GhosttyTerminalSelectLineOptions::whitespace_len"]
        [::std::mem::offset_of!(GhosttyTerminalSelectLineOptions, whitespace_len) - 40usize];
    ["Offset of field: GhosttyTerminalSelectLineOptions::semantic_prompt_boundary"][::std::mem::offset_of!(
        GhosttyTerminalSelectLineOptions,
        semantic_prompt_boundary
    ) - 48usize];
};
impl Default for GhosttyTerminalSelectLineOptions {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Options for one-shot formatting of a terminal selection.\n\n This is a sized struct. Use GHOSTTY_INIT_SIZED() to initialize it.\n\n If selection is NULL, the terminal's current active selection is used.\n If selection is non-NULL, that caller-provided snapshot selection is used.\n\n The selection is formatted from the terminal's active screen using the same\n formatting semantics as GhosttyFormatter. For copy/clipboard behavior\n matching Ghostty's Screen.selectionString(), use plain output with unwrap\n and trim both set to true.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalSelectionFormatOptions {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyTerminalSelectionFormatOptions)."]
    pub size: usize,
    #[doc = " Output format to emit."]
    pub emit: GhosttyFormatterFormat,
    #[doc = " Whether to unwrap soft-wrapped lines."]
    pub unwrap: bool,
    #[doc = " Whether to trim trailing whitespace on non-blank lines."]
    pub trim: bool,
    #[doc = " Optional selection to format.\n\n If NULL, the terminal's current active selection is used. If the terminal\n has no active selection, formatting returns GHOSTTY_NO_VALUE.\n\n If non-NULL, the pointed-to selection must be a valid snapshot selection\n for this terminal and must obey GhosttySelection lifetime rules."]
    pub selection: *const GhosttySelection,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalSelectionFormatOptions"]
        [::std::mem::size_of::<GhosttyTerminalSelectionFormatOptions>() - 24usize];
    ["Alignment of GhosttyTerminalSelectionFormatOptions"]
        [::std::mem::align_of::<GhosttyTerminalSelectionFormatOptions>() - 8usize];
    ["Offset of field: GhosttyTerminalSelectionFormatOptions::size"]
        [::std::mem::offset_of!(GhosttyTerminalSelectionFormatOptions, size) - 0usize];
    ["Offset of field: GhosttyTerminalSelectionFormatOptions::emit"]
        [::std::mem::offset_of!(GhosttyTerminalSelectionFormatOptions, emit) - 8usize];
    ["Offset of field: GhosttyTerminalSelectionFormatOptions::unwrap"]
        [::std::mem::offset_of!(GhosttyTerminalSelectionFormatOptions, unwrap) - 12usize];
    ["Offset of field: GhosttyTerminalSelectionFormatOptions::trim"]
        [::std::mem::offset_of!(GhosttyTerminalSelectionFormatOptions, trim) - 13usize];
    ["Offset of field: GhosttyTerminalSelectionFormatOptions::selection"]
        [::std::mem::offset_of!(GhosttyTerminalSelectionFormatOptions, selection) - 16usize];
};
impl Default for GhosttyTerminalSelectionFormatOptions {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Start is before end in top-left to bottom-right order."]
pub const GhosttySelectionOrder_GHOSTTY_SELECTION_ORDER_FORWARD: GhosttySelectionOrder = 0;
#[doc = " End is before start in top-left to bottom-right order."]
pub const GhosttySelectionOrder_GHOSTTY_SELECTION_ORDER_REVERSE: GhosttySelectionOrder = 1;
#[doc = " Rectangular selection from top-right to bottom-left."]
pub const GhosttySelectionOrder_GHOSTTY_SELECTION_ORDER_MIRRORED_FORWARD: GhosttySelectionOrder = 2;
#[doc = " Rectangular selection from bottom-left to top-right."]
pub const GhosttySelectionOrder_GHOSTTY_SELECTION_ORDER_MIRRORED_REVERSE: GhosttySelectionOrder = 3;
#[doc = " Rectangular selection from bottom-left to top-right."]
pub const GhosttySelectionOrder_GHOSTTY_SELECTION_ORDER_MAX_VALUE: GhosttySelectionOrder =
    2147483647;
#[doc = " Ordering of a selection's endpoints in terminal coordinates.\n\n Mirrored orders are only produced by rectangular selections whose start\n and end endpoints are on opposite diagonal corners that are not simple\n top-left-to-bottom-right or bottom-right-to-top-left orderings.\n\n @ingroup selection"]
pub type GhosttySelectionOrder = ::std::os::raw::c_uint;
#[doc = " Move left to the previous non-empty cell, wrapping upward."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_LEFT: GhosttySelectionAdjust = 0;
#[doc = " Move right to the next non-empty cell, wrapping downward."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_RIGHT: GhosttySelectionAdjust = 1;
#[doc = " Move up one row at the current column, or to the beginning of the\n line if already at the top."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_UP: GhosttySelectionAdjust = 2;
#[doc = " Move down to the next non-blank row at the current column, or to the\n end of the line if none exists."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_DOWN: GhosttySelectionAdjust = 3;
#[doc = " Move to the top-left cell of the screen."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_HOME: GhosttySelectionAdjust = 4;
#[doc = " Move to the right edge of the last non-blank row on the screen."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_END: GhosttySelectionAdjust = 5;
#[doc = " Move up by one terminal page height, or to home if that would move\n past the top."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_PAGE_UP: GhosttySelectionAdjust = 6;
#[doc = " Move down by one terminal page height, or to end if that would move\n past the bottom."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_PAGE_DOWN: GhosttySelectionAdjust = 7;
#[doc = " Move to the left edge of the current line."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_BEGINNING_OF_LINE:
    GhosttySelectionAdjust = 8;
#[doc = " Move to the right edge of the current line."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_END_OF_LINE: GhosttySelectionAdjust = 9;
#[doc = " Move to the right edge of the current line."]
pub const GhosttySelectionAdjust_GHOSTTY_SELECTION_ADJUST_MAX_VALUE: GhosttySelectionAdjust =
    2147483647;
#[doc = " Operation used to adjust a selection endpoint.\n\n Adjustment mutates the selection's logical end endpoint, not whichever\n endpoint is visually bottom/right. This preserves keyboard and drag\n behavior for both forward and reversed selections.\n\n @ingroup selection"]
pub type GhosttySelectionAdjust = ::std::os::raw::c_uint;
#[doc = " Cell-granular drag selection."]
pub const GhosttySelectionGestureBehavior_GHOSTTY_SELECTION_GESTURE_BEHAVIOR_CELL:
    GhosttySelectionGestureBehavior = 0;
#[doc = " Word selection on press and word-granular drag selection."]
pub const GhosttySelectionGestureBehavior_GHOSTTY_SELECTION_GESTURE_BEHAVIOR_WORD:
    GhosttySelectionGestureBehavior = 1;
#[doc = " Line selection on press and line-granular drag selection."]
pub const GhosttySelectionGestureBehavior_GHOSTTY_SELECTION_GESTURE_BEHAVIOR_LINE:
    GhosttySelectionGestureBehavior = 2;
#[doc = " Semantic command output selection on press and drag."]
pub const GhosttySelectionGestureBehavior_GHOSTTY_SELECTION_GESTURE_BEHAVIOR_OUTPUT:
    GhosttySelectionGestureBehavior = 3;
#[doc = " Semantic command output selection on press and drag."]
pub const GhosttySelectionGestureBehavior_GHOSTTY_SELECTION_GESTURE_BEHAVIOR_MAX_VALUE:
    GhosttySelectionGestureBehavior = 2147483647;
#[doc = " Selection behavior chosen for a gesture's click sequence.\n\n @ingroup selection"]
pub type GhosttySelectionGestureBehavior = ::std::os::raw::c_uint;
#[doc = " Selection behaviors for single-, double-, and triple-click gestures.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySelectionGestureBehaviors {
    #[doc = " Behavior for single-click selection gestures."]
    pub single_click: GhosttySelectionGestureBehavior,
    #[doc = " Behavior for double-click selection gestures."]
    pub double_click: GhosttySelectionGestureBehavior,
    #[doc = " Behavior for triple-click selection gestures."]
    pub triple_click: GhosttySelectionGestureBehavior,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySelectionGestureBehaviors"]
        [::std::mem::size_of::<GhosttySelectionGestureBehaviors>() - 12usize];
    ["Alignment of GhosttySelectionGestureBehaviors"]
        [::std::mem::align_of::<GhosttySelectionGestureBehaviors>() - 4usize];
    ["Offset of field: GhosttySelectionGestureBehaviors::single_click"]
        [::std::mem::offset_of!(GhosttySelectionGestureBehaviors, single_click) - 0usize];
    ["Offset of field: GhosttySelectionGestureBehaviors::double_click"]
        [::std::mem::offset_of!(GhosttySelectionGestureBehaviors, double_click) - 4usize];
    ["Offset of field: GhosttySelectionGestureBehaviors::triple_click"]
        [::std::mem::offset_of!(GhosttySelectionGestureBehaviors, triple_click) - 8usize];
};
impl Default for GhosttySelectionGestureBehaviors {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Display geometry used to interpret selection gesture drag events.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttySelectionGestureGeometry {
    #[doc = " Number of columns in the rendered terminal grid. Must be non-zero."]
    pub columns: u32,
    #[doc = " Width of one terminal cell in surface pixels. Must be non-zero."]
    pub cell_width: u32,
    #[doc = " Left padding before the terminal grid begins in surface pixels."]
    pub padding_left: u32,
    #[doc = " Height of the rendered terminal surface in surface pixels. Must be non-zero."]
    pub screen_height: u32,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySelectionGestureGeometry"]
        [::std::mem::size_of::<GhosttySelectionGestureGeometry>() - 16usize];
    ["Alignment of GhosttySelectionGestureGeometry"]
        [::std::mem::align_of::<GhosttySelectionGestureGeometry>() - 4usize];
    ["Offset of field: GhosttySelectionGestureGeometry::columns"]
        [::std::mem::offset_of!(GhosttySelectionGestureGeometry, columns) - 0usize];
    ["Offset of field: GhosttySelectionGestureGeometry::cell_width"]
        [::std::mem::offset_of!(GhosttySelectionGestureGeometry, cell_width) - 4usize];
    ["Offset of field: GhosttySelectionGestureGeometry::padding_left"]
        [::std::mem::offset_of!(GhosttySelectionGestureGeometry, padding_left) - 8usize];
    ["Offset of field: GhosttySelectionGestureGeometry::screen_height"]
        [::std::mem::offset_of!(GhosttySelectionGestureGeometry, screen_height) - 12usize];
};
#[doc = " No selection autoscroll is requested."]
pub const GhosttySelectionGestureAutoscroll_GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_NONE:
    GhosttySelectionGestureAutoscroll = 0;
#[doc = " Selection dragging should autoscroll the viewport upward."]
pub const GhosttySelectionGestureAutoscroll_GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_UP:
    GhosttySelectionGestureAutoscroll = 1;
#[doc = " Selection dragging should autoscroll the viewport downward."]
pub const GhosttySelectionGestureAutoscroll_GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_DOWN:
    GhosttySelectionGestureAutoscroll = 2;
#[doc = " Selection dragging should autoscroll the viewport downward."]
pub const GhosttySelectionGestureAutoscroll_GHOSTTY_SELECTION_GESTURE_AUTOSCROLL_MAX_VALUE:
    GhosttySelectionGestureAutoscroll = 2147483647;
#[doc = " Current autoscroll direction for an active selection drag gesture.\n\n @ingroup selection"]
pub type GhosttySelectionGestureAutoscroll = ::std::os::raw::c_uint;
#[doc = " Current click count: uint8_t*. 0 means inactive."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_CLICK_COUNT:
    GhosttySelectionGestureData = 0;
#[doc = " Whether the current/last left-click gesture has dragged: bool*."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_DRAGGED:
    GhosttySelectionGestureData = 1;
#[doc = " Current autoscroll request: GhosttySelectionGestureAutoscroll*."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_AUTOSCROLL:
    GhosttySelectionGestureData = 2;
#[doc = " Current gesture behavior: GhosttySelectionGestureBehavior*."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_BEHAVIOR:
    GhosttySelectionGestureData = 3;
#[doc = " Current left-click anchor: GhosttyGridRef*.\n\n Returns GHOSTTY_NO_VALUE if there is no valid active anchor. On success,\n writes an untracked GhosttyGridRef snapshot with normal GhosttyGridRef\n lifetime rules."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_ANCHOR:
    GhosttySelectionGestureData = 4;
#[doc = " Current left-click anchor: GhosttyGridRef*.\n\n Returns GHOSTTY_NO_VALUE if there is no valid active anchor. On success,\n writes an untracked GhosttyGridRef snapshot with normal GhosttyGridRef\n lifetime rules."]
pub const GhosttySelectionGestureData_GHOSTTY_SELECTION_GESTURE_DATA_MAX_VALUE:
    GhosttySelectionGestureData = 2147483647;
#[doc = " Data fields readable from a selection gesture with\n ghostty_selection_gesture_get().\n\n @ingroup selection"]
pub type GhosttySelectionGestureData = ::std::os::raw::c_uint;
#[doc = " Press event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_PRESS:
    GhosttySelectionGestureEventType = 0;
#[doc = " Release event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_RELEASE:
    GhosttySelectionGestureEventType = 1;
#[doc = " Drag event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_DRAG:
    GhosttySelectionGestureEventType = 2;
#[doc = " Autoscroll tick event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_AUTOSCROLL_TICK:
    GhosttySelectionGestureEventType = 3;
#[doc = " Deep press event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_DEEP_PRESS:
    GhosttySelectionGestureEventType = 4;
#[doc = " Deep press event for ghostty_selection_gesture_event()."]
pub const GhosttySelectionGestureEventType_GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_MAX_VALUE:
    GhosttySelectionGestureEventType = 2147483647;
#[doc = " Selection gesture event type.\n\n The event type is fixed when the event is created. Each event type documents\n which options are valid and which options are required by gesture operations.\n\n @ingroup selection"]
pub type GhosttySelectionGestureEventType = ::std::os::raw::c_uint;
#[doc = " Grid reference under the pointer: GhosttyGridRef*.\n\n Required for PRESS and DRAG events. Optional for RELEASE events; when unset\n or cleared, release records that the pointer did not map to a valid cell."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF:
    GhosttySelectionGestureEventOption = 0;
#[doc = " Surface-space pointer position: GhosttySurfacePosition*.\n\n Valid for PRESS, DRAG, and AUTOSCROLL_TICK."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_POSITION:
    GhosttySelectionGestureEventOption = 1;
#[doc = " Maximum repeat-click distance in pixels: double*."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_DISTANCE:
    GhosttySelectionGestureEventOption = 2;
#[doc = " Optional monotonic event time in nanoseconds: uint64_t*.\n\n If unset, press treats the event as untimed and only single-click behavior\n is available."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_TIME_NS:
    GhosttySelectionGestureEventOption = 3;
#[doc = " Maximum interval between repeat clicks in nanoseconds: uint64_t*."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_INTERVAL_NS : GhosttySelectionGestureEventOption = 4 ;
#[doc = " Word-boundary codepoints: GhosttyCodepoints*.\n\n The codepoints are copied into event-owned storage when set. If unset,\n operations that need word boundaries use Ghostty's defaults.\n\n Valid for PRESS, DRAG, AUTOSCROLL_TICK, and DEEP_PRESS."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_WORD_BOUNDARY_CODEPOINTS : GhosttySelectionGestureEventOption = 5 ;
#[doc = " Selection behavior table: GhosttySelectionGestureBehaviors*.\n\n If unset, press uses the default behavior table: cell, word, line."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_BEHAVIORS:
    GhosttySelectionGestureEventOption = 6;
#[doc = " Whether a drag or autoscroll tick should produce a rectangular selection: bool*."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_RECTANGLE:
    GhosttySelectionGestureEventOption = 7;
#[doc = " Drag display geometry: GhosttySelectionGestureGeometry*. Required for DRAG and AUTOSCROLL_TICK."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY:
    GhosttySelectionGestureEventOption = 8;
#[doc = " Viewport coordinate for an autoscroll tick: GhosttyPointCoordinate*. Required for AUTOSCROLL_TICK."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_VIEWPORT:
    GhosttySelectionGestureEventOption = 9;
#[doc = " Viewport coordinate for an autoscroll tick: GhosttyPointCoordinate*. Required for AUTOSCROLL_TICK."]
pub const GhosttySelectionGestureEventOption_GHOSTTY_SELECTION_GESTURE_EVENT_OPT_MAX_VALUE:
    GhosttySelectionGestureEventOption = 2147483647;
#[doc = " Options stored on a reusable selection gesture event.\n\n Passing NULL as the value to ghostty_selection_gesture_event_set() clears the\n corresponding option.\n\n @ingroup selection"]
pub type GhosttySelectionGestureEventOption = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Create a reusable selection gesture event object.\n\n @param allocator Allocator, or NULL for the default allocator\n @param out_event Receives the created event handle\n @param type Event type. This is fixed for the lifetime of the event.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if out_event is\n         NULL or type is invalid, or GHOSTTY_OUT_OF_MEMORY if allocation fails\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_event_new(
        allocator: *const GhosttyAllocator,
        out_event: *mut GhosttySelectionGestureEvent,
        type_: GhosttySelectionGestureEventType,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a selection gesture event object.\n\n Passing NULL is allowed and is a no-op.\n\n @param event Selection gesture event handle to free\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_event_free(event: GhosttySelectionGestureEvent);
}
unsafe extern "C" {
    #[doc = " Set or clear an option on a selection gesture event.\n\n The value type depends on option and is documented by\n GhosttySelectionGestureEventOption. Passing NULL for value clears the option.\n\n @param event Selection gesture event handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param option Event option to set or clear\n @param value Pointer to the input value for option, or NULL to clear\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY if copying\n         event-owned data fails, or GHOSTTY_INVALID_VALUE if event, option, or\n         value is invalid\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_event_set(
        event: GhosttySelectionGestureEvent,
        option: GhosttySelectionGestureEventOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Apply a selection gesture event and return the resulting selection snapshot.\n\n This dispatches to the gesture operation matching the event's fixed type.\n For GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_PRESS, the event must have\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF set before calling this function.\n All other press options use their initialized defaults when unset or cleared.\n\n For GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_RELEASE, only\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF is valid. It is optional; if unset or\n cleared, release records that the pointer did not map to a valid cell. Release\n events update gesture state but do not produce a selection, so this function\n returns GHOSTTY_NO_VALUE after applying them.\n\n For GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_DRAG,\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF and\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY are required. Position,\n rectangle, and word-boundary codepoints are optional and use initialized\n defaults when unset or cleared.\n\n For GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_AUTOSCROLL_TICK,\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_VIEWPORT and\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY are required. Position,\n rectangle, and word-boundary codepoints are optional and use initialized\n defaults when unset or cleared.\n\n For GHOSTTY_SELECTION_GESTURE_EVENT_TYPE_DEEP_PRESS, only\n GHOSTTY_SELECTION_GESTURE_EVENT_OPT_WORD_BOUNDARY_CODEPOINTS is valid. It is\n optional and uses initialized defaults when unset or cleared.\n\n The returned selection is not installed as the terminal's current selection.\n It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param gesture Selection gesture handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param terminal Terminal used to interpret and update gesture state\n @param event Selection gesture event handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param[out] out_selection On success, receives the resulting selection. May\n             be NULL to apply the event and discard the selection result.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the event does not\n         currently produce a selection, GHOSTTY_OUT_OF_MEMORY if tracking\n         gesture state fails, or GHOSTTY_INVALID_VALUE if gesture, terminal,\n         event, or required event data is invalid\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_event(
        gesture: GhosttySelectionGesture,
        terminal: GhosttyTerminal,
        event: GhosttySelectionGestureEvent,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a selection gesture object.\n\n The gesture stores mutable state for terminal text selection gestures. The\n gesture is not bound to a terminal at creation time; terminal-dependent APIs\n take the terminal explicitly.\n\n @param allocator Allocator, or NULL for the default allocator\n @param out_gesture Receives the created gesture handle\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if out_gesture is\n         NULL, or GHOSTTY_OUT_OF_MEMORY if allocation fails\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_new(
        allocator: *const GhosttyAllocator,
        out_gesture: *mut GhosttySelectionGesture,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a selection gesture object.\n\n This releases any tracked terminal references owned by the gesture using the\n provided terminal, then frees the gesture object. Passing NULL for gesture is\n allowed and is a no-op.\n\n If the terminal is still alive, pass the terminal most recently used with the\n gesture so any tracked terminal references can be released correctly. If the\n terminal has already been freed, pass NULL for terminal; the terminal's page\n storage has already released the underlying tracked references, so the\n gesture wrapper can be safely discarded without touching the stale terminal\n state.\n\n @param gesture Selection gesture handle to free\n @param terminal Terminal used to release tracked gesture state, or NULL if\n                 the terminal has already been freed\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_free(
        gesture: GhosttySelectionGesture,
        terminal: GhosttyTerminal,
    );
}
unsafe extern "C" {
    #[doc = " Reset any active selection gesture state.\n\n This cancels the active click sequence and releases any tracked terminal\n references owned by the gesture without freeing the gesture object.\n Passing NULL is allowed and is a no-op.\n\n @param gesture Selection gesture handle to reset\n @param terminal Terminal used to release tracked gesture state\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_reset(
        gesture: GhosttySelectionGesture,
        terminal: GhosttyTerminal,
    );
}
unsafe extern "C" {
    #[doc = " Read data from a selection gesture.\n\n The type of value depends on data and is documented by\n GhosttySelectionGestureData. For GHOSTTY_SELECTION_GESTURE_DATA_ANCHOR,\n the returned GhosttyGridRef is an untracked snapshot with normal grid-ref\n lifetime rules.\n\n @param gesture Selection gesture handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param terminal Terminal used to validate terminal-backed gesture state\n @param data Data field to read\n @param value Output pointer whose type depends on data\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the requested data\n         has no value, or GHOSTTY_INVALID_VALUE if gesture, terminal, data, or\n         value is invalid\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_get(
        gesture: GhosttySelectionGesture,
        terminal: GhosttyTerminal,
        data: GhosttySelectionGestureData,
        value: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Read multiple data fields from a selection gesture in a single call.\n\n This is an optimization over calling ghostty_selection_gesture_get() multiple\n times. Each entry in values must point to storage of the type documented by\n the corresponding GhosttySelectionGestureData key.\n\n If any individual read fails, the function returns that error and writes the\n index of the failing key to out_written when out_written is non-NULL. On\n success, out_written receives count when non-NULL.\n\n @param gesture Selection gesture handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param terminal Terminal used to validate terminal-backed gesture state\n @param count Number of data fields to read\n @param keys Data fields to read (must not be NULL)\n @param values Output pointers corresponding to keys (must not be NULL)\n @param out_written Optional number of fields read, or failing index on error\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if a requested data\n         field has no value, or GHOSTTY_INVALID_VALUE if gesture, terminal,\n         keys, values, or a value pointer is invalid\n\n @ingroup selection"]
    pub fn ghostty_selection_gesture_get_multi(
        gesture: GhosttySelectionGesture,
        terminal: GhosttyTerminal,
        count: usize,
        keys: *const GhosttySelectionGestureData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Derive a word selection snapshot from a terminal grid reference.\n\n The returned selection is not installed as the terminal's current\n selection. It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param options Word-selection options\n @param[out] out_selection On success, receives the derived selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the valid ref has\n         no selectable word content, or GHOSTTY_INVALID_VALUE if the\n         terminal, options, ref, codepoint pointer, or output pointer are\n         invalid.\n\n @ingroup selection"]
    pub fn ghostty_terminal_select_word(
        terminal: GhosttyTerminal,
        options: *const GhosttyTerminalSelectWordOptions,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Derive the nearest word selection snapshot between two terminal grid refs.\n\n Starting at options->start, this searches toward options->end (inclusive)\n and returns the first selectable word found using Ghostty's word-selection\n rules.\n\n This is useful for implementing double-click-and-drag selection in a UI. If\n a user double-clicks one word and drags across spaces or punctuation toward\n another word, selecting only the word directly under the current pointer can\n flicker or collapse when the pointer is between words. Instead, ask for the\n nearest word between the original click and the drag point, ask again in the\n reverse direction, and combine the two word bounds into the drag selection.\n\n @snippet c-vt-selection/src/main.c selection-word-between\n\n The returned selection is not installed as the terminal's current\n selection. It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param options Word-between-selection options\n @param[out] out_selection On success, receives the derived selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if there is no\n         selectable word content between the valid refs, or\n         GHOSTTY_INVALID_VALUE if the terminal, options, refs, codepoint\n         pointer, or output pointer are invalid.\n\n @ingroup selection"]
    pub fn ghostty_terminal_select_word_between(
        terminal: GhosttyTerminal,
        options: *const GhosttyTerminalSelectWordBetweenOptions,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Derive a line selection snapshot from a terminal grid reference.\n\n The returned selection is not installed as the terminal's current\n selection. It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param options Line-selection options\n @param[out] out_selection On success, receives the derived selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the valid ref has\n         no selectable line content, or GHOSTTY_INVALID_VALUE if the\n         terminal, options, ref, codepoint pointer, or output pointer are\n         invalid.\n\n @ingroup selection"]
    pub fn ghostty_terminal_select_line(
        terminal: GhosttyTerminal,
        options: *const GhosttyTerminalSelectLineOptions,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Derive a selection snapshot covering all selectable terminal content.\n\n The returned selection is not installed as the terminal's current\n selection. It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param[out] out_selection On success, receives the derived selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if there is no\n         selectable content, or GHOSTTY_INVALID_VALUE if the terminal or\n         output pointer is invalid.\n\n @ingroup selection"]
    pub fn ghostty_terminal_select_all(
        terminal: GhosttyTerminal,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Derive a command-output selection snapshot from a terminal grid reference.\n\n The returned selection is not installed as the terminal's current\n selection. It is a snapshot with the same lifetime rules as GhosttySelection.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param ref Grid reference within command output to select\n @param[out] out_selection On success, receives the derived selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the valid ref is\n         not selectable command output, or GHOSTTY_INVALID_VALUE if the\n         terminal, ref, or output pointer is invalid.\n\n @ingroup selection"]
    pub fn ghostty_terminal_select_output(
        terminal: GhosttyTerminal,
        ref_: GhosttyGridRef,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Format a terminal selection into a caller-provided buffer.\n\n This is a one-shot convenience API for formatting either the terminal's\n active selection or a caller-provided GhosttySelection without explicitly\n creating a GhosttyFormatter.\n\n Pass NULL for buf to query the required output size. In that case,\n out_written receives the required size and the function returns\n GHOSTTY_OUT_OF_SPACE.\n\n If buf is too small, the function returns GHOSTTY_OUT_OF_SPACE and writes\n the required size to out_written. The caller can then retry with a larger\n buffer.\n\n If options.selection is NULL and the terminal has no active selection, the\n function returns GHOSTTY_NO_VALUE.\n\n @param terminal The terminal to read from (must not be NULL)\n @param options Selection formatting options\n @param buf Output buffer, or NULL to query required size\n @param buf_len Length of buf in bytes\n @param out_written Number of bytes written, or required size on\n                    GHOSTTY_OUT_OF_SPACE (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_format_buf(
        terminal: GhosttyTerminal,
        options: GhosttyTerminalSelectionFormatOptions,
        buf: *mut u8,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Format a terminal selection into an allocated buffer.\n\n This is a one-shot convenience API for formatting either the terminal's\n active selection or a caller-provided GhosttySelection without explicitly\n creating a GhosttyFormatter.\n\n The returned buffer is allocated using allocator, or the default allocator\n if NULL is passed. The caller owns the returned buffer and must free it with\n ghostty_free(), passing the same allocator and returned length.\n\n The returned bytes are not NUL-terminated. This supports plain text, VT, and\n HTML uniformly as byte output.\n\n If options.selection is NULL and the terminal has no active selection, the\n function returns GHOSTTY_NO_VALUE and leaves out_ptr as NULL and out_len as 0.\n\n @param terminal The terminal to read from (must not be NULL)\n @param allocator Allocator used for the returned buffer, or NULL for the default allocator\n @param options Selection formatting options\n @param out_ptr Receives the allocated output buffer (must not be NULL)\n @param out_len Receives the output length in bytes (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_format_alloc(
        terminal: GhosttyTerminal,
        allocator: *const GhosttyAllocator,
        options: GhosttyTerminalSelectionFormatOptions,
        out_ptr: *mut *mut u8,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Adjust a selection snapshot using terminal selection semantics.\n\n This mutates the caller-provided GhosttySelection in place. The logical end\n endpoint is always moved, regardless of whether the selection is forward or\n reversed visually. The input selection remains a snapshot: after adjustment,\n call ghostty_terminal_set() with GHOSTTY_TERMINAL_OPT_SELECTION to install it\n as the terminal-owned selection if desired.\n\n The selection's start and end grid refs must both be valid untracked\n snapshots for the given terminal's currently active screen. In practice,\n they must come from that terminal and screen, and no mutating terminal call\n may have occurred since the refs were produced or reconstructed from\n tracked refs. Passing refs from another terminal, another screen, or stale\n refs violates this precondition.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param selection Selection snapshot to adjust in place\n @param adjustment The adjustment operation to apply\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal,\n         selection, or adjustment are invalid. Selection reference validity\n         is a precondition and is not checked.\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_adjust(
        terminal: GhosttyTerminal,
        selection: *mut GhosttySelection,
        adjustment: GhosttySelectionAdjust,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the current endpoint ordering of a selection snapshot.\n\n The selection's start and end grid refs must both be valid untracked\n snapshots for the given terminal's currently active screen. In practice,\n they must come from that terminal and screen, and no mutating terminal call\n may have occurred since the refs were produced or reconstructed from\n tracked refs. Passing refs from another terminal, another screen, or stale\n refs violates this precondition.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param selection Selection snapshot to inspect\n @param[out] out_order On success, receives the selection order\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal,\n         selection, or output pointer are invalid. Selection reference\n         validity is a precondition and is not checked.\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_order(
        terminal: GhosttyTerminal,
        selection: *const GhosttySelection,
        out_order: *mut GhosttySelectionOrder,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Return a selection snapshot with endpoints ordered as requested.\n\n Use GHOSTTY_SELECTION_ORDER_FORWARD to get top-left to bottom-right bounds,\n and GHOSTTY_SELECTION_ORDER_REVERSE to get bottom-right to top-left bounds.\n Mirrored desired orders are accepted but normalized the same as forward.\n The output selection is a fresh untracked snapshot and is not installed as\n the terminal's current selection.\n\n The selection's start and end grid refs must both be valid untracked\n snapshots for the given terminal's currently active screen. In practice,\n they must come from that terminal and screen, and no mutating terminal call\n may have occurred since the refs were produced or reconstructed from\n tracked refs. Passing refs from another terminal, another screen, or stale\n refs violates this precondition.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param selection Selection snapshot to order\n @param desired Desired endpoint order\n @param[out] out_selection On success, receives the ordered selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal,\n         selection, desired order, or output pointer are invalid. Selection\n         reference validity is a precondition and is not checked.\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_ordered(
        terminal: GhosttyTerminal,
        selection: *const GhosttySelection,
        desired: GhosttySelectionOrder,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Test whether a terminal point is inside a selection snapshot.\n\n This uses the same selection semantics as the terminal, including\n rectangular/block selections and linear selections spanning multiple rows.\n\n The selection's start and end grid refs must both be valid untracked\n snapshots for the given terminal's currently active screen. In practice,\n they must come from that terminal and screen, and no mutating terminal call\n may have occurred since the refs were produced or reconstructed from\n tracked refs. Passing refs from another terminal, another screen, or stale\n refs violates this precondition.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param selection Selection snapshot to inspect\n @param point Point to test for containment\n @param[out] out_contains On success, receives whether point is inside selection\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal,\n         selection, point, or output pointer are invalid. Selection reference\n         validity is a precondition and is not checked.\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_contains(
        terminal: GhosttyTerminal,
        selection: *const GhosttySelection,
        point: GhosttyPoint,
        out_contains: *mut bool,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Test whether two selection snapshots are equal.\n\n Equality uses the terminal's internal selection semantics: both endpoint\n pins must match and both selections must have the same rectangular/block\n state. This avoids requiring callers to compare raw GhosttyGridRef internals.\n\n Both selections' start and end grid refs must be valid untracked snapshots\n for the given terminal's currently active screen. In practice, they must\n come from that terminal and screen, and no mutating terminal call may have\n occurred since the refs were produced or reconstructed from tracked refs.\n Passing refs from another terminal, another screen, or stale refs violates\n this precondition.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param a First selection snapshot to compare\n @param b Second selection snapshot to compare\n @param[out] out_equal On success, receives whether the selections are equal\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal,\n         selections, or output pointer are invalid. Selection reference\n         validity is a precondition and is not checked.\n\n @ingroup selection"]
    pub fn ghostty_terminal_selection_equal(
        terminal: GhosttyTerminal,
        a: *const GhosttySelection,
        b: *const GhosttySelection,
        out_equal: *mut bool,
    ) -> GhosttyResult;
}
#[doc = " A packed 16-bit terminal mode.\n\n Encodes a mode value (bits 0–14) and an ANSI flag (bit 15) into a\n single 16-bit integer. Use the inline helper functions to construct\n and inspect modes rather than manipulating bits directly."]
pub type GhosttyMode = u16;
#[doc = " Mode is not recognized"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_NOT_RECOGNIZED: GhosttyModeReportState = 0;
#[doc = " Mode is set (enabled)"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_SET: GhosttyModeReportState = 1;
#[doc = " Mode is reset (disabled)"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_RESET: GhosttyModeReportState = 2;
#[doc = " Mode is permanently set"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_PERMANENTLY_SET: GhosttyModeReportState = 3;
#[doc = " Mode is permanently reset"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_PERMANENTLY_RESET: GhosttyModeReportState = 4;
#[doc = " Mode is permanently reset"]
pub const GhosttyModeReportState_GHOSTTY_MODE_REPORT_MAX_VALUE: GhosttyModeReportState = 2147483647;
#[doc = " DECRPM report state values.\n\n These correspond to the Ps2 parameter in a DECRPM response\n sequence (CSI ? Ps1 ; Ps2 $ y)."]
pub type GhosttyModeReportState = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Encode a DECRPM (DEC Private Mode Report) response sequence.\n\n Writes a mode report escape sequence into the provided buffer.\n The generated sequence has the form:\n - DEC private mode: CSI ? Ps1 ; Ps2 $ y\n - ANSI mode:        CSI Ps1 ; Ps2 $ y\n\n If the buffer is too small, the function returns GHOSTTY_OUT_OF_SPACE\n and writes the required buffer size to @p out_written. The caller can\n then retry with a sufficiently sized buffer.\n\n @param mode The mode identifying the mode to report on\n @param state The report state for this mode\n @param buf Output buffer to write the encoded sequence into (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_written On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if the buffer\n         is too small"]
    pub fn ghostty_mode_report_encode(
        mode: GhosttyMode,
        state: GhosttyModeReportState,
        buf: *mut ::std::os::raw::c_char,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " In-band size report (mode 2048): ESC [ 48 ; rows ; cols ; height ; width t"]
pub const GhosttySizeReportStyle_GHOSTTY_SIZE_REPORT_MODE_2048: GhosttySizeReportStyle = 0;
#[doc = " XTWINOPS text area size in pixels: ESC [ 4 ; height ; width t"]
pub const GhosttySizeReportStyle_GHOSTTY_SIZE_REPORT_CSI_14_T: GhosttySizeReportStyle = 1;
#[doc = " XTWINOPS cell size in pixels: ESC [ 6 ; height ; width t"]
pub const GhosttySizeReportStyle_GHOSTTY_SIZE_REPORT_CSI_16_T: GhosttySizeReportStyle = 2;
#[doc = " XTWINOPS text area size in characters: ESC [ 8 ; rows ; cols t"]
pub const GhosttySizeReportStyle_GHOSTTY_SIZE_REPORT_CSI_18_T: GhosttySizeReportStyle = 3;
#[doc = " XTWINOPS text area size in characters: ESC [ 8 ; rows ; cols t"]
pub const GhosttySizeReportStyle_GHOSTTY_SIZE_REPORT_STYLE_MAX_VALUE: GhosttySizeReportStyle =
    2147483647;
#[doc = " Size report style.\n\n Determines the output format for the terminal size report."]
pub type GhosttySizeReportStyle = ::std::os::raw::c_uint;
#[doc = " Terminal size information for encoding size reports."]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttySizeReportSize {
    #[doc = " Terminal row count in cells."]
    pub rows: u16,
    #[doc = " Terminal column count in cells."]
    pub columns: u16,
    #[doc = " Width of a single terminal cell in pixels."]
    pub cell_width: u32,
    #[doc = " Height of a single terminal cell in pixels."]
    pub cell_height: u32,
}
