#[doc = " Combined rendering geometry for a placement in a single sized struct.\n\n Combines the results of ghostty_kitty_graphics_placement_pixel_size(),\n ghostty_kitty_graphics_placement_grid_size(),\n ghostty_kitty_graphics_placement_viewport_pos(), and\n ghostty_kitty_graphics_placement_source_rect() into one call. This is\n an optimization over calling those four functions individually,\n particularly useful in environments with high per-call overhead such\n as FFI or Cgo.\n\n This struct uses the sized-struct ABI pattern. Initialize with\n GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo) before calling\n ghostty_kitty_graphics_placement_render_info().\n\n @ingroup kitty_graphics"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyKittyGraphicsPlacementRenderInfo {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyKittyGraphicsPlacementRenderInfo)."]
    pub size: usize,
    #[doc = " Rendered width in pixels."]
    pub pixel_width: u32,
    #[doc = " Rendered height in pixels."]
    pub pixel_height: u32,
    #[doc = " Number of grid columns the placement occupies."]
    pub grid_cols: u32,
    #[doc = " Number of grid rows the placement occupies."]
    pub grid_rows: u32,
    #[doc = " Viewport-relative column (may be negative for partially visible placements)."]
    pub viewport_col: i32,
    #[doc = " Viewport-relative row (may be negative for partially visible placements)."]
    pub viewport_row: i32,
    #[doc = " False when the placement is fully off-screen or virtual."]
    pub viewport_visible: bool,
    #[doc = " Resolved source rectangle x origin in pixels."]
    pub source_x: u32,
    #[doc = " Resolved source rectangle y origin in pixels."]
    pub source_y: u32,
    #[doc = " Resolved source rectangle width in pixels."]
    pub source_width: u32,
    #[doc = " Resolved source rectangle height in pixels."]
    pub source_height: u32,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyKittyGraphicsPlacementRenderInfo"]
        [::std::mem::size_of::<GhosttyKittyGraphicsPlacementRenderInfo>() - 56usize];
    ["Alignment of GhosttyKittyGraphicsPlacementRenderInfo"]
        [::std::mem::align_of::<GhosttyKittyGraphicsPlacementRenderInfo>() - 8usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::size"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, size) - 0usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::pixel_width"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, pixel_width) - 8usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::pixel_height"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, pixel_height) - 12usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::grid_cols"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, grid_cols) - 16usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::grid_rows"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, grid_rows) - 20usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::viewport_col"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, viewport_col) - 24usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::viewport_row"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, viewport_row) - 28usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::viewport_visible"][::std::mem::offset_of!(
        GhosttyKittyGraphicsPlacementRenderInfo,
        viewport_visible
    ) - 32usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::source_x"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, source_x) - 36usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::source_y"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, source_y) - 40usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::source_width"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, source_width) - 44usize];
    ["Offset of field: GhosttyKittyGraphicsPlacementRenderInfo::source_height"]
        [::std::mem::offset_of!(GhosttyKittyGraphicsPlacementRenderInfo, source_height) - 48usize];
};
unsafe extern "C" {
    #[doc = " Get data from a kitty graphics storage instance.\n\n The output pointer must be of the appropriate type for the requested\n data kind.\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n @param graphics The kitty graphics handle\n @param data The type of data to extract\n @param[out] out Pointer to store the extracted data\n @return GHOSTTY_SUCCESS on success\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_get(
        graphics: GhosttyKittyGraphics,
        data: GhosttyKittyGraphicsData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Look up a Kitty graphics image by its image ID.\n\n Returns NULL if no image with the given ID exists or if Kitty graphics\n are disabled at build time.\n\n @param graphics The kitty graphics handle\n @param image_id The image ID to look up\n @return An opaque image handle, or NULL if not found\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_image(
        graphics: GhosttyKittyGraphics,
        image_id: u32,
    ) -> GhosttyKittyGraphicsImage;
}
unsafe extern "C" {
    #[doc = " Get data from a Kitty graphics image.\n\n The output pointer must be of the appropriate type for the requested\n data kind.\n\n @param image The image handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param data The data kind to query\n @param[out] out Pointer to receive the queried value\n @return GHOSTTY_SUCCESS on success\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_image_get(
        image: GhosttyKittyGraphicsImage,
        data: GhosttyKittyGraphicsImageData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from a Kitty graphics image in a single call.\n\n This is an optimization over calling ghostty_kitty_graphics_image_get()\n repeatedly, particularly useful in environments with high per-call\n overhead such as FFI or Cgo.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n The type of each values[i] pointer must match the output type\n documented for keys[i].\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param image The image handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_image_get_multi(
        image: GhosttyKittyGraphicsImage,
        count: usize,
        keys: *const GhosttyKittyGraphicsImageData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a new placement iterator instance.\n\n All fields except the allocator are left undefined until populated\n via ghostty_kitty_graphics_get() with\n GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param[out] out_iterator On success, receives the created iterator handle\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n         failure\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_iterator_new(
        allocator: *const GhosttyAllocator,
        out_iterator: *mut GhosttyKittyGraphicsPlacementIterator,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a placement iterator.\n\n @param iterator The iterator handle to free (may be NULL)\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_iterator_free(
        iterator: GhosttyKittyGraphicsPlacementIterator,
    );
}
unsafe extern "C" {
    #[doc = " Set an option on a placement iterator.\n\n Use GHOSTTY_KITTY_GRAPHICS_PLACEMENT_ITERATOR_OPTION_LAYER with a\n GhosttyKittyPlacementLayer value to filter placements by z-layer.\n The filter is applied during iteration: ghostty_kitty_graphics_placement_next()\n will skip placements that do not match the configured layer.\n\n The default layer is GHOSTTY_KITTY_PLACEMENT_LAYER_ALL (no filtering).\n\n @param iterator The iterator handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param option The option to set\n @param value Pointer to the value (type depends on option; NULL returns\n              GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_iterator_set(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        option: GhosttyKittyGraphicsPlacementIteratorOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Advance the placement iterator to the next placement.\n\n If a layer filter has been set via\n ghostty_kitty_graphics_placement_iterator_set(), only placements\n matching that layer are returned.\n\n @param iterator The iterator handle (may be NULL)\n @return true if advanced to the next placement, false if at the end\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_next(
        iterator: GhosttyKittyGraphicsPlacementIterator,
    ) -> bool;
}
unsafe extern "C" {
    #[doc = " Get data from the current placement in a placement iterator.\n\n Call ghostty_kitty_graphics_placement_next() at least once before\n calling this function.\n\n @param iterator The iterator handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param data The data kind to query\n @param[out] out Pointer to receive the queried value\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the\n         iterator is NULL or not positioned on a placement\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_get(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        data: GhosttyKittyGraphicsPlacementData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from the current placement in a single call.\n\n This is an optimization over calling ghostty_kitty_graphics_placement_get()\n repeatedly, particularly useful in environments with high per-call\n overhead such as FFI or Cgo.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n The type of each values[i] pointer must match the output type\n documented for keys[i].\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param iterator The iterator handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_get_multi(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        count: usize,
        keys: *const GhosttyKittyGraphicsPlacementData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Compute the grid rectangle occupied by the current placement.\n\n Uses the placement's pin, the image dimensions, and the terminal's\n cell/pixel geometry to calculate the bounding rectangle. Virtual\n placements (unicode placeholders) return GHOSTTY_NO_VALUE.\n\n @param terminal The terminal handle\n @param image The image handle for this placement's image\n @param iterator The placement iterator positioned on a placement\n @param[out] out_selection On success, receives the bounding rectangle\n             as a selection with rectangle=true\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if any handle\n         is NULL or the iterator is not positioned, GHOSTTY_NO_VALUE for\n         virtual placements or when Kitty graphics are disabled\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_rect(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        terminal: GhosttyTerminal,
        out_selection: *mut GhosttySelection,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Compute the rendered pixel size of the current placement.\n\n Takes into account the placement's source rectangle, specified\n columns/rows, and aspect ratio to calculate the final rendered\n pixel dimensions.\n\n @param iterator The placement iterator positioned on a placement\n @param image The image handle for this placement's image\n @param terminal The terminal handle\n @param[out] out_width On success, receives the width in pixels\n @param[out] out_height On success, receives the height in pixels\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if any handle\n         is NULL or the iterator is not positioned, GHOSTTY_NO_VALUE when\n         Kitty graphics are disabled\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_pixel_size(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        terminal: GhosttyTerminal,
        out_width: *mut u32,
        out_height: *mut u32,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Compute the grid cell size of the current placement.\n\n Returns the number of columns and rows that the placement occupies\n in the terminal grid. If the placement specifies explicit columns\n and rows, those are returned directly; otherwise they are calculated\n from the pixel size and cell dimensions.\n\n @param iterator The placement iterator positioned on a placement\n @param image The image handle for this placement's image\n @param terminal The terminal handle\n @param[out] out_cols On success, receives the number of columns\n @param[out] out_rows On success, receives the number of rows\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if any handle\n         is NULL or the iterator is not positioned, GHOSTTY_NO_VALUE when\n         Kitty graphics are disabled\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_grid_size(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        terminal: GhosttyTerminal,
        out_cols: *mut u32,
        out_rows: *mut u32,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the viewport-relative grid position of the current placement.\n\n Converts the placement's internal pin to viewport-relative column and\n row coordinates. The returned coordinates represent the top-left\n corner of the placement in the viewport's grid coordinate space.\n\n The row value can be negative when the placement's origin has\n scrolled above the top of the viewport. For example, a 4-row\n image that has scrolled up by 2 rows returns row=-2, meaning\n its top 2 rows are above the visible area but its bottom 2 rows\n are still on screen. Embedders should use these coordinates\n directly when computing the destination rectangle for rendering;\n the embedder is responsible for clipping the portion of the image\n that falls outside the viewport.\n\n Returns GHOSTTY_SUCCESS for any placement that is at least\n partially visible in the viewport. Returns GHOSTTY_NO_VALUE when\n the placement is completely outside the viewport (its bottom edge\n is above the viewport or its top edge is at or below the last\n viewport row), or when the placement is a virtual (unicode\n placeholder) placement.\n\n @param iterator The placement iterator positioned on a placement\n @param image The image handle for this placement's image\n @param terminal The terminal handle\n @param[out] out_col On success, receives the viewport-relative column\n @param[out] out_row On success, receives the viewport-relative row\n             (may be negative for partially visible placements)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if fully\n         off-screen or virtual, GHOSTTY_INVALID_VALUE if any handle\n         is NULL or the iterator is not positioned\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_viewport_pos(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        terminal: GhosttyTerminal,
        out_col: *mut i32,
        out_row: *mut i32,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the resolved source rectangle for the current placement.\n\n Applies kitty protocol semantics: a width or height of 0 in the\n placement means \"use the full image dimension\", and the resulting\n rectangle is clamped to the actual image bounds. The returned\n values are in pixels and are ready to use for texture sampling.\n\n @param iterator The placement iterator positioned on a placement\n @param image The image handle for this placement's image\n @param[out] out_x Source rect x origin in pixels\n @param[out] out_y Source rect y origin in pixels\n @param[out] out_width Source rect width in pixels\n @param[out] out_height Source rect height in pixels\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if any\n         handle is NULL or the iterator is not positioned\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_source_rect(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        out_x: *mut u32,
        out_y: *mut u32,
        out_width: *mut u32,
        out_height: *mut u32,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get all rendering geometry for a placement in a single call.\n\n Combines pixel size, grid size, viewport position, and source\n rectangle into one struct. Initialize with\n GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo).\n\n When viewport_visible is false, the placement is fully off-screen\n or is a virtual placement; viewport_col and viewport_row may\n contain meaningless values in that case.\n\n @param iterator The iterator positioned on a placement\n @param image The image handle for this placement's image\n @param terminal The terminal handle\n @param[out] out_info Pointer to receive the rendering geometry\n @return GHOSTTY_SUCCESS on success\n\n @ingroup kitty_graphics"]
    pub fn ghostty_kitty_graphics_placement_render_info(
        iterator: GhosttyKittyGraphicsPlacementIterator,
        image: GhosttyKittyGraphicsImage,
        terminal: GhosttyTerminal,
        out_info: *mut GhosttyKittyGraphicsPlacementRenderInfo,
    ) -> GhosttyResult;
}
#[doc = " Perform one bounded compression step suitable for idle scheduling."]
pub const GhosttyTerminalCompressionMode_GHOSTTY_TERMINAL_COMPRESSION_MODE_INCREMENTAL:
    GhosttyTerminalCompressionMode = 0;
#[doc = " Synchronously inspect every currently eligible page."]
pub const GhosttyTerminalCompressionMode_GHOSTTY_TERMINAL_COMPRESSION_MODE_FULL:
    GhosttyTerminalCompressionMode = 1;
#[doc = " Synchronously inspect every currently eligible page."]
pub const GhosttyTerminalCompressionMode_GHOSTTY_TERMINAL_COMPRESSION_MODE_MAX_VALUE:
    GhosttyTerminalCompressionMode = 2147483647;
#[doc = " Amount of compression work to perform before returning.\n\n @ingroup terminal"]
pub type GhosttyTerminalCompressionMode = ::std::os::raw::c_int;
#[doc = " Retained-mapping reclamation is unavailable on this target."]
pub const GhosttyTerminalCompressionResult_GHOSTTY_TERMINAL_COMPRESSION_RESULT_UNSUPPORTED:
    GhosttyTerminalCompressionResult = 0;
#[doc = " More incremental compression work remains."]
pub const GhosttyTerminalCompressionResult_GHOSTTY_TERMINAL_COMPRESSION_RESULT_PENDING:
    GhosttyTerminalCompressionResult = 1;
#[doc = " The pass has no continuation to schedule."]
pub const GhosttyTerminalCompressionResult_GHOSTTY_TERMINAL_COMPRESSION_RESULT_COMPLETE:
    GhosttyTerminalCompressionResult = 2;
#[doc = " The pass has no continuation to schedule."]
pub const GhosttyTerminalCompressionResult_GHOSTTY_TERMINAL_COMPRESSION_RESULT_MAX_VALUE:
    GhosttyTerminalCompressionResult = 2147483647;
#[doc = " Scheduling result from terminal compression.\n\n @ingroup terminal"]
pub type GhosttyTerminalCompressionResult = ::std::os::raw::c_int;
#[doc = " Scroll to the top of the scrollback."]
pub const GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_TOP:
    GhosttyTerminalScrollViewportTag = 0;
#[doc = " Scroll to the bottom (active area)."]
pub const GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_BOTTOM:
    GhosttyTerminalScrollViewportTag = 1;
#[doc = " Scroll by a delta amount (up is negative)."]
pub const GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_DELTA:
    GhosttyTerminalScrollViewportTag = 2;
#[doc = " Scroll to an absolute row offset from the top of the scrollable\n area. Row 0 is the top of the scrollback and the requested row\n becomes the first visible row of the viewport. The value is\n clamped so the viewport never scrolls beyond the top of the\n active area. If the terminal has no scrollback (e.g. the\n alternate screen is active), the viewport always remains on the\n active area.\n\n This is the same row space as the offset field of\n GhosttyTerminalScrollbar, so a scrollbar position obtained from\n GHOSTTY_TERMINAL_DATA_SCROLLBAR round-trips cleanly."]
pub const GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_ROW:
    GhosttyTerminalScrollViewportTag = 3;
#[doc = " Scroll to an absolute row offset from the top of the scrollable\n area. Row 0 is the top of the scrollback and the requested row\n becomes the first visible row of the viewport. The value is\n clamped so the viewport never scrolls beyond the top of the\n active area. If the terminal has no scrollback (e.g. the\n alternate screen is active), the viewport always remains on the\n active area.\n\n This is the same row space as the offset field of\n GhosttyTerminalScrollbar, so a scrollbar position obtained from\n GHOSTTY_TERMINAL_DATA_SCROLLBAR round-trips cleanly."]
pub const GhosttyTerminalScrollViewportTag_GHOSTTY_SCROLL_VIEWPORT_MAX_VALUE:
    GhosttyTerminalScrollViewportTag = 2147483647;
#[doc = " Scroll viewport behavior tag.\n\n @ingroup terminal"]
pub type GhosttyTerminalScrollViewportTag = ::std::os::raw::c_int;
#[doc = " Scroll viewport value.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Copy, Clone)]
pub union GhosttyTerminalScrollViewportValue {
    #[doc = " Scroll delta (only used with GHOSTTY_SCROLL_VIEWPORT_DELTA). Up is negative."]
    pub delta: isize,
    #[doc = " Absolute row offset (only used with GHOSTTY_SCROLL_VIEWPORT_ROW)."]
    pub row: usize,
    #[doc = " Padding for ABI compatibility. Do not use."]
    pub _padding: [u64; 2usize],
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalScrollViewportValue"]
        [::std::mem::size_of::<GhosttyTerminalScrollViewportValue>() - 16usize];
    ["Alignment of GhosttyTerminalScrollViewportValue"]
        [::std::mem::align_of::<GhosttyTerminalScrollViewportValue>() - 8usize];
    ["Offset of field: GhosttyTerminalScrollViewportValue::delta"]
        [::std::mem::offset_of!(GhosttyTerminalScrollViewportValue, delta) - 0usize];
    ["Offset of field: GhosttyTerminalScrollViewportValue::row"]
        [::std::mem::offset_of!(GhosttyTerminalScrollViewportValue, row) - 0usize];
    ["Offset of field: GhosttyTerminalScrollViewportValue::_padding"]
        [::std::mem::offset_of!(GhosttyTerminalScrollViewportValue, _padding) - 0usize];
};
impl Default for GhosttyTerminalScrollViewportValue {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Tagged union for scroll viewport behavior.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Copy, Clone)]
pub struct GhosttyTerminalScrollViewport {
    pub tag: GhosttyTerminalScrollViewportTag,
    pub value: GhosttyTerminalScrollViewportValue,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalScrollViewport"]
        [::std::mem::size_of::<GhosttyTerminalScrollViewport>() - 24usize];
    ["Alignment of GhosttyTerminalScrollViewport"]
        [::std::mem::align_of::<GhosttyTerminalScrollViewport>() - 8usize];
    ["Offset of field: GhosttyTerminalScrollViewport::tag"]
        [::std::mem::offset_of!(GhosttyTerminalScrollViewport, tag) - 0usize];
    ["Offset of field: GhosttyTerminalScrollViewport::value"]
        [::std::mem::offset_of!(GhosttyTerminalScrollViewport, value) - 8usize];
};
impl Default for GhosttyTerminalScrollViewport {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " The primary (normal) screen."]
pub const GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_PRIMARY: GhosttyTerminalScreen = 0;
#[doc = " The alternate screen."]
pub const GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_ALTERNATE: GhosttyTerminalScreen = 1;
#[doc = " The alternate screen."]
pub const GhosttyTerminalScreen_GHOSTTY_TERMINAL_SCREEN_MAX_VALUE: GhosttyTerminalScreen =
    2147483647;
#[doc = " Terminal screen identifier.\n\n Identifies which screen buffer is active in the terminal.\n\n @ingroup terminal"]
pub type GhosttyTerminalScreen = ::std::os::raw::c_int;
#[doc = " Bar cursor (DECSCUSR 5, 6)."]
pub const GhosttyTerminalCursorStyle_GHOSTTY_TERMINAL_CURSOR_STYLE_BAR: GhosttyTerminalCursorStyle =
    0;
#[doc = " Block cursor (DECSCUSR 1, 2)."]
pub const GhosttyTerminalCursorStyle_GHOSTTY_TERMINAL_CURSOR_STYLE_BLOCK:
    GhosttyTerminalCursorStyle = 1;
#[doc = " Underline cursor (DECSCUSR 3, 4)."]
pub const GhosttyTerminalCursorStyle_GHOSTTY_TERMINAL_CURSOR_STYLE_UNDERLINE:
    GhosttyTerminalCursorStyle = 2;
#[doc = " Hollow block cursor."]
pub const GhosttyTerminalCursorStyle_GHOSTTY_TERMINAL_CURSOR_STYLE_BLOCK_HOLLOW:
    GhosttyTerminalCursorStyle = 3;
#[doc = " Hollow block cursor."]
pub const GhosttyTerminalCursorStyle_GHOSTTY_TERMINAL_CURSOR_STYLE_MAX_VALUE:
    GhosttyTerminalCursorStyle = 2147483647;
#[doc = " Visual style of the terminal cursor.\n\n @ingroup terminal"]
pub type GhosttyTerminalCursorStyle = ::std::os::raw::c_int;
#[doc = " Scrollbar state for the terminal viewport.\n\n Represents the scrollable area dimensions needed to render a scrollbar.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyTerminalScrollbar {
    #[doc = " Total size of the scrollable area in rows."]
    pub total: u64,
    #[doc = " Offset into the total area that the viewport is at."]
    pub offset: u64,
    #[doc = " Length of the visible area in rows."]
    pub len: u64,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalScrollbar"]
        [::std::mem::size_of::<GhosttyTerminalScrollbar>() - 24usize];
    ["Alignment of GhosttyTerminalScrollbar"]
        [::std::mem::align_of::<GhosttyTerminalScrollbar>() - 8usize];
    ["Offset of field: GhosttyTerminalScrollbar::total"]
        [::std::mem::offset_of!(GhosttyTerminalScrollbar, total) - 0usize];
    ["Offset of field: GhosttyTerminalScrollbar::offset"]
        [::std::mem::offset_of!(GhosttyTerminalScrollbar, offset) - 8usize];
    ["Offset of field: GhosttyTerminalScrollbar::len"]
        [::std::mem::offset_of!(GhosttyTerminalScrollbar, len) - 16usize];
};
#[doc = " Callback function type for bell.\n\n Called when the terminal receives a BEL character (0x07).\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n\n @ingroup terminal"]
pub type GhosttyTerminalBellFn = ::std::option::Option<
    unsafe extern "C" fn(terminal: GhosttyTerminal, userdata: *mut ::std::os::raw::c_void),
>;
#[doc = " Application Program Command (APC)."]
pub const GhosttyTerminalUnknownSequenceTag_GHOSTTY_TERMINAL_UNKNOWN_SEQUENCE_APC:
    GhosttyTerminalUnknownSequenceTag = 0;
#[doc = " Application Program Command (APC)."]
pub const GhosttyTerminalUnknownSequenceTag_GHOSTTY_TERMINAL_UNKNOWN_SEQUENCE_MAX_VALUE:
    GhosttyTerminalUnknownSequenceTag = 2147483647;
#[doc = " Unsupported terminal sequence tags.\n\n Only APC sequences are currently reported. Additional sequence types may\n be added without changing the callback shape.\n\n @ingroup terminal"]
pub type GhosttyTerminalUnknownSequenceTag = ::std::os::raw::c_int;
#[doc = " An unsupported string terminal sequence.\n\n The content is borrowed and valid only for the callback duration. It\n contains the bytes between the sequence introducer and terminator, may\n contain arbitrary binary data, and is not null-terminated.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalUnknownStringSequence {
    #[doc = " Whether content was shortened by the byte limit or allocation failure."]
    pub truncated: bool,
    #[doc = " Retained sequence content."]
    pub content: GhosttyString,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalUnknownStringSequence"]
        [::std::mem::size_of::<GhosttyTerminalUnknownStringSequence>() - 24usize];
    ["Alignment of GhosttyTerminalUnknownStringSequence"]
        [::std::mem::align_of::<GhosttyTerminalUnknownStringSequence>() - 8usize];
    ["Offset of field: GhosttyTerminalUnknownStringSequence::truncated"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownStringSequence, truncated) - 0usize];
    ["Offset of field: GhosttyTerminalUnknownStringSequence::content"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownStringSequence, content) - 8usize];
};
impl Default for GhosttyTerminalUnknownStringSequence {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Unsupported terminal sequence value.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Copy, Clone)]
pub union GhosttyTerminalUnknownSequenceValue {
    #[doc = " Application Program Command (APC)."]
    pub apc: GhosttyTerminalUnknownStringSequence,
    #[doc = " Padding for ABI compatibility. Do not use.\n\n 128 bytes leaves room for future structured sequence payloads, such as\n CSI with borrowed parameter, separator, and intermediate arrays, without\n changing the tagged union's ABI."]
    pub _padding: [u64; 16usize],
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalUnknownSequenceValue"]
        [::std::mem::size_of::<GhosttyTerminalUnknownSequenceValue>() - 128usize];
    ["Alignment of GhosttyTerminalUnknownSequenceValue"]
        [::std::mem::align_of::<GhosttyTerminalUnknownSequenceValue>() - 8usize];
    ["Offset of field: GhosttyTerminalUnknownSequenceValue::apc"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownSequenceValue, apc) - 0usize];
    ["Offset of field: GhosttyTerminalUnknownSequenceValue::_padding"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownSequenceValue, _padding) - 0usize];
};
impl Default for GhosttyTerminalUnknownSequenceValue {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " An unsupported terminal sequence.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Copy, Clone)]
pub struct GhosttyTerminalUnknownSequence {
    pub tag: GhosttyTerminalUnknownSequenceTag,
    pub value: GhosttyTerminalUnknownSequenceValue,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalUnknownSequence"]
        [::std::mem::size_of::<GhosttyTerminalUnknownSequence>() - 136usize];
    ["Alignment of GhosttyTerminalUnknownSequence"]
        [::std::mem::align_of::<GhosttyTerminalUnknownSequence>() - 8usize];
    ["Offset of field: GhosttyTerminalUnknownSequence::tag"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownSequence, tag) - 0usize];
    ["Offset of field: GhosttyTerminalUnknownSequence::value"]
        [::std::mem::offset_of!(GhosttyTerminalUnknownSequence, value) - 8usize];
};
impl Default for GhosttyTerminalUnknownSequence {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Callback function type for unsupported terminal sequences.\n\n Called synchronously for normally terminated sequences whose identifier is\n not supported by the active terminal handler. Aborted sequences, malformed\n recognized commands, and explicitly disabled known protocols are ignored.\n\n Capture must also be enabled with a nonzero\n GHOSTTY_TERMINAL_OPT_UNKNOWN_MAX_BYTES value. Installing this callback alone\n does not retain sequence content or allocate memory.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param sequence Borrowed unsupported sequence\n\n @ingroup terminal"]
pub type GhosttyTerminalUnknownSequenceFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        sequence: *const GhosttyTerminalUnknownSequence,
    ),
>;
#[doc = " The standard system clipboard."]
pub const GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_STANDARD: GhosttyClipboardLocation =
    0;
#[doc = " The selection clipboard."]
pub const GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_SELECTION: GhosttyClipboardLocation =
    1;
#[doc = " The primary selection clipboard."]
pub const GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_PRIMARY: GhosttyClipboardLocation = 2;
#[doc = " The primary selection clipboard."]
pub const GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_MAX_VALUE: GhosttyClipboardLocation =
    2147483647;
#[doc = " Clipboard destination for a clipboard write.\n\n Protocol-specific destination identifiers are normalized to these values\n before the clipboard write callback is invoked.\n\n @ingroup terminal"]
pub type GhosttyClipboardLocation = ::std::os::raw::c_int;
#[doc = " One MIME representation in a clipboard write.\n\n Both strings are borrowed and valid only for the duration of the callback.\n The data is binary-safe and has already been decoded from any protocol-level\n encoding. A zero-length data string is an explicit empty representation; it\n does not clear the clipboard.\n\n This struct has a frozen layout and will not gain fields in future versions.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyClipboardContent {
    #[doc = " MIME type of the representation."]
    pub mime: GhosttyString,
    #[doc = " Decoded, binary-safe representation data."]
    pub data: GhosttyString,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardContent"][::std::mem::size_of::<GhosttyClipboardContent>() - 32usize];
    ["Alignment of GhosttyClipboardContent"]
        [::std::mem::align_of::<GhosttyClipboardContent>() - 8usize];
    ["Offset of field: GhosttyClipboardContent::mime"]
        [::std::mem::offset_of!(GhosttyClipboardContent, mime) - 0usize];
    ["Offset of field: GhosttyClipboardContent::data"]
        [::std::mem::offset_of!(GhosttyClipboardContent, data) - 16usize];
};
impl Default for GhosttyClipboardContent {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " The clipboard write completed successfully."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS:
    GhosttyClipboardWriteResult = 0;
#[doc = " The clipboard write was denied by policy or the user."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_DENIED:
    GhosttyClipboardWriteResult = 1;
#[doc = " The destination or one or more representations are unsupported."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED:
    GhosttyClipboardWriteResult = 2;
#[doc = " The clipboard is temporarily unavailable."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_BUSY:
    GhosttyClipboardWriteResult = 3;
#[doc = " One or more representations contain invalid data."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA:
    GhosttyClipboardWriteResult = 4;
#[doc = " The clipboard write failed due to an I/O error."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_IO_ERROR:
    GhosttyClipboardWriteResult = 5;
#[doc = " The clipboard write failed due to an I/O error."]
pub const GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_MAX_VALUE:
    GhosttyClipboardWriteResult = 2147483647;
#[doc = " Result of a clipboard write reply.\n\n @ingroup terminal"]
pub type GhosttyClipboardWriteResult = ::std::os::raw::c_int;
#[doc = " The reply to a clipboard write request.\n\n This is a sized struct; set `size` to `sizeof(GhosttyClipboardWriteReply)`.\n The reply is borrowed only for the duration of the reply call and may be\n freed as soon as it returns.\n\n The result answers the program with the matching protocol status for\n protocols with a write acknowledgement (OSC 5522: DONE, EPERM, ENOSYS,\n EBUSY, EINVAL, EIO); protocols without one (OSC 52, OSC 1337 Copy)\n discard the reply. `remember` is ignored on any result other than\n GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyClipboardWriteReply {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Outcome of the write."]
    pub result: GhosttyClipboardWriteResult,
    #[doc = " Record a session grant so future requests from the same program skip\n the permission prompt. Only honored on success when\n GhosttyClipboardWrite::can_remember is set."]
    pub remember: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardWriteReply"]
        [::std::mem::size_of::<GhosttyClipboardWriteReply>() - 16usize];
    ["Alignment of GhosttyClipboardWriteReply"]
        [::std::mem::align_of::<GhosttyClipboardWriteReply>() - 8usize];
    ["Offset of field: GhosttyClipboardWriteReply::size"]
        [::std::mem::offset_of!(GhosttyClipboardWriteReply, size) - 0usize];
    ["Offset of field: GhosttyClipboardWriteReply::result"]
        [::std::mem::offset_of!(GhosttyClipboardWriteReply, result) - 8usize];
    ["Offset of field: GhosttyClipboardWriteReply::remember"]
        [::std::mem::offset_of!(GhosttyClipboardWriteReply, remember) - 12usize];
};
impl Default for GhosttyClipboardWriteReply {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Function type used to answer a clipboard write request. Obtained from\n GhosttyClipboardWrite::reply; see that struct for the contract.\n\n @param write The request being answered\n @param reply The reply, borrowed only for the duration of this call\n\n @ingroup terminal"]
pub type GhosttyClipboardWriteReplyFn = ::std::option::Option<
    unsafe extern "C" fn(
        write: *const GhosttyClipboardWrite,
        reply: *const GhosttyClipboardWriteReply,
    ),
>;
#[doc = " A synchronous request to write clipboard contents.\n\n This is a sized struct. The callback must only access fields present in the\n size reported by `size`. The request, contents array, MIME strings, and\n data strings are all borrowed and valid only for the callback duration.\n\n All entries in `contents` are representations of the same logical value\n and must be committed atomically. A `contents_len` of zero requests that\n the destination be cleared. This is distinct from a content entry whose data\n has zero length.\n\n The write is answered by calling `reply` with this request and a\n GhosttyClipboardWriteReply. This must happen within the clipboard write\n request callback. This struct is only valid during that time. Calling\n `reply` more than once is safely ignored. Returning without replying\n denies the write.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyClipboardWrite {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Clipboard destination."]
    pub location: GhosttyClipboardLocation,
    #[doc = " Borrowed array of MIME representations."]
    pub contents: *const GhosttyClipboardContent,
    #[doc = " Number of entries in contents; zero means clear the destination."]
    pub contents_len: usize,
    #[doc = " Name of the writing program for permission prompts, if the protocol\n carries one. Empty otherwise."]
    pub name: GhosttyString,
    #[doc = " True if the terminal already holds a session grant for this request\n The embedder should skip any permission prompt and perform the write."]
    pub granted: bool,
    #[doc = " True if the program supplied a session password, so the embedder may\n offer to remember the user's decision through\n GhosttyClipboardWriteReply::remember. When false, remember is ignored."]
    pub can_remember: bool,
    #[doc = " Terminal-owned reply state. Do not access."]
    pub ctx: *const ::std::os::raw::c_void,
    #[doc = " Answer the write; see the struct documentation."]
    pub reply: GhosttyClipboardWriteReplyFn,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardWrite"][::std::mem::size_of::<GhosttyClipboardWrite>() - 72usize];
    ["Alignment of GhosttyClipboardWrite"]
        [::std::mem::align_of::<GhosttyClipboardWrite>() - 8usize];
    ["Offset of field: GhosttyClipboardWrite::size"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, size) - 0usize];
    ["Offset of field: GhosttyClipboardWrite::location"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, location) - 8usize];
    ["Offset of field: GhosttyClipboardWrite::contents"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, contents) - 16usize];
    ["Offset of field: GhosttyClipboardWrite::contents_len"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, contents_len) - 24usize];
    ["Offset of field: GhosttyClipboardWrite::name"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, name) - 32usize];
    ["Offset of field: GhosttyClipboardWrite::granted"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, granted) - 48usize];
    ["Offset of field: GhosttyClipboardWrite::can_remember"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, can_remember) - 49usize];
    ["Offset of field: GhosttyClipboardWrite::ctx"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, ctx) - 56usize];
    ["Offset of field: GhosttyClipboardWrite::reply"]
        [::std::mem::offset_of!(GhosttyClipboardWrite, reply) - 64usize];
};
impl Default for GhosttyClipboardWrite {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Callback function type for clipboard_write.\n\n The embedder may ask for permission to write or perform the write\n async, but the callback itself is synchronous and the reply function\n must be called during the lifetime of this function. While this callback\n is active the VT stream is paused.\n\n Answer by calling `write->reply(write, &reply)` before returning. See\n GhosttyClipboardWrite for the full contract.\n\n The request may carry an optional program name requesting the write\n and the state of prior permission granted. If `can_remember` is set\n the response may set the `remember` flag and future requests from this\n same program will be \"granted\" and the embedder can skip permission\n requests.\n\n Clipboard read requests (OSC 52 \"?\" and OSC 5522 reads) are delivered\n to GhosttyTerminalClipboardReadFn instead.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param write Borrowed atomic clipboard write request\n\n @ingroup terminal"]
pub type GhosttyTerminalClipboardWriteFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        write: *const GhosttyClipboardWrite,
    ),
>;
#[doc = " The clipboard was read; the reply carries its contents."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_SUCCESS:
    GhosttyClipboardReadResult = 0;
#[doc = " The clipboard read was denied by policy or the user."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_DENIED:
    GhosttyClipboardReadResult = 1;
#[doc = " The embedder cannot read this clipboard."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_UNSUPPORTED:
    GhosttyClipboardReadResult = 2;
#[doc = " The clipboard is temporarily unavailable."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_BUSY:
    GhosttyClipboardReadResult = 3;
#[doc = " Reading the clipboard failed due to an I/O error."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_IO_ERROR:
    GhosttyClipboardReadResult = 4;
#[doc = " Reading the clipboard failed due to an I/O error."]
pub const GhosttyClipboardReadResult_GHOSTTY_CLIPBOARD_READ_RESULT_MAX_VALUE:
    GhosttyClipboardReadResult = 2147483647;
#[doc = " Result of a clipboard read reply.\n\n @ingroup terminal"]
pub type GhosttyClipboardReadResult = ::std::os::raw::c_int;
#[doc = " The reply to a clipboard read request.\n\n This is a sized struct; set `size` to `sizeof(GhosttyClipboardReadReply)`.\n All arrays and the strings they point to are borrowed only for the\n duration of the reply call and may be freed as soon as it returns.\n\n Any result other than GHOSTTY_CLIPBOARD_READ_RESULT_SUCCESS answers the\n program with an empty clipboard (OSC 52) or the matching protocol status\n (OSC 5522: EPERM, ENOSYS, EBUSY, EIO); the other fields are ignored in\n that case. On success, `contents` should carry one representation per\n requested MIME type (GhosttyClipboardRead::mimes) that the clipboard\n has; unrequested representations are ignored. Protocols that carry a\n single text value (OSC 52) use the first entry with a text MIME type\n such as \"text/plain\".\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyClipboardReadReply {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Outcome of the read."]
    pub result: GhosttyClipboardReadResult,
    #[doc = " Borrowed array of MIME representations of the clipboard contents."]
    pub contents: *const GhosttyClipboardContent,
    #[doc = " Number of entries in contents."]
    pub contents_len: usize,
    #[doc = " Borrowed array of all MIME types available on the clipboard. Only\n used when GhosttyClipboardRead::list is set; may be NULL otherwise."]
    pub available: *const GhosttyString,
    #[doc = " Number of entries in available."]
    pub available_len: usize,
    #[doc = " Record a session grant so future requests from the same program skip\n the permission prompt. Only honored on success when\n GhosttyClipboardRead::can_remember is set."]
    pub remember: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardReadReply"]
        [::std::mem::size_of::<GhosttyClipboardReadReply>() - 56usize];
    ["Alignment of GhosttyClipboardReadReply"]
        [::std::mem::align_of::<GhosttyClipboardReadReply>() - 8usize];
    ["Offset of field: GhosttyClipboardReadReply::size"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, size) - 0usize];
    ["Offset of field: GhosttyClipboardReadReply::result"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, result) - 8usize];
    ["Offset of field: GhosttyClipboardReadReply::contents"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, contents) - 16usize];
    ["Offset of field: GhosttyClipboardReadReply::contents_len"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, contents_len) - 24usize];
    ["Offset of field: GhosttyClipboardReadReply::available"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, available) - 32usize];
    ["Offset of field: GhosttyClipboardReadReply::available_len"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, available_len) - 40usize];
    ["Offset of field: GhosttyClipboardReadReply::remember"]
        [::std::mem::offset_of!(GhosttyClipboardReadReply, remember) - 48usize];
};
impl Default for GhosttyClipboardReadReply {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Function type used to answer a clipboard read request. Obtained from\n GhosttyClipboardRead::reply; see that struct for the contract.\n\n @param read The request being answered\n @param reply The reply, borrowed only for the duration of this call\n\n @ingroup terminal"]
pub type GhosttyClipboardReadReplyFn = ::std::option::Option<
    unsafe extern "C" fn(
        read: *const GhosttyClipboardRead,
        reply: *const GhosttyClipboardReadReply,
    ),
>;
