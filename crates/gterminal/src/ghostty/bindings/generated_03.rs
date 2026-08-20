#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySizeReportSize"][::std::mem::size_of::<GhosttySizeReportSize>() - 12usize];
    ["Alignment of GhosttySizeReportSize"]
        [::std::mem::align_of::<GhosttySizeReportSize>() - 4usize];
    ["Offset of field: GhosttySizeReportSize::rows"]
        [::std::mem::offset_of!(GhosttySizeReportSize, rows) - 0usize];
    ["Offset of field: GhosttySizeReportSize::columns"]
        [::std::mem::offset_of!(GhosttySizeReportSize, columns) - 2usize];
    ["Offset of field: GhosttySizeReportSize::cell_width"]
        [::std::mem::offset_of!(GhosttySizeReportSize, cell_width) - 4usize];
    ["Offset of field: GhosttySizeReportSize::cell_height"]
        [::std::mem::offset_of!(GhosttySizeReportSize, cell_height) - 8usize];
};
unsafe extern "C" {
    #[doc = " Encode a terminal size report into an escape sequence.\n\n Encodes a size report in the format specified by @p style into the\n provided buffer.\n\n If the buffer is too small, the function returns GHOSTTY_OUT_OF_SPACE\n and writes the required buffer size to @p out_written. The caller can\n then retry with a sufficiently sized buffer.\n\n @param style The size report format to encode\n @param size Terminal size information\n @param buf Output buffer to write the encoded sequence into (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_written On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if the buffer\n         is too small"]
    pub fn ghostty_size_report_encode(
        style: GhosttySizeReportStyle,
        size: GhosttySizeReportSize,
        buf: *mut ::std::os::raw::c_char,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " Invalid / sentinel value."]
pub const GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_INVALID: GhosttyKittyGraphicsData =
    0;
#[doc = " Populate a pre-allocated placement iterator with placement data from\n the storage. Iterator data is only valid as long as the underlying\n terminal is not mutated.\n\n Output type: GhosttyKittyGraphicsPlacementIterator *"]
pub const GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR:
    GhosttyKittyGraphicsData = 1;
#[doc = " Generation stamp of the last content mutation to this storage:\n any image transmit/replace, placement add, or delete. Zero means\n the storage has never been mutated (and is therefore empty).\n\n If the generation is unchanged since a previous query, the set of\n placements and all image data are identical, so placement iteration\n and image staleness checks can be skipped entirely. Note that\n placement *geometry* may still have changed (scrolling and resizing\n move placements without changing the storage contents), so rendering\n geometry such as ghostty_kitty_graphics_placement_render_info()\n must still be recomputed for frames marked dirty.\n\n Stamps are unique and monotonically increasing process-wide: a\n value observed from any storage never recurs for different content,\n even across screen switches (main vs. alternate screen have\n independent storages) or terminal resets. It is therefore safe to\n key caches on this value alone.\n\n Output type: uint64_t *"]
pub const GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION:
    GhosttyKittyGraphicsData = 2;
#[doc = " Generation stamp of the last content mutation to this storage:\n any image transmit/replace, placement add, or delete. Zero means\n the storage has never been mutated (and is therefore empty).\n\n If the generation is unchanged since a previous query, the set of\n placements and all image data are identical, so placement iteration\n and image staleness checks can be skipped entirely. Note that\n placement *geometry* may still have changed (scrolling and resizing\n move placements without changing the storage contents), so rendering\n geometry such as ghostty_kitty_graphics_placement_render_info()\n must still be recomputed for frames marked dirty.\n\n Stamps are unique and monotonically increasing process-wide: a\n value observed from any storage never recurs for different content,\n even across screen switches (main vs. alternate screen have\n independent storages) or terminal resets. It is therefore safe to\n key caches on this value alone.\n\n Output type: uint64_t *"]
pub const GhosttyKittyGraphicsData_GHOSTTY_KITTY_GRAPHICS_DATA_MAX_VALUE: GhosttyKittyGraphicsData =
    2147483647;
#[doc = " Queryable data kinds for ghostty_kitty_graphics_get().\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyGraphicsData = ::std::os::raw::c_uint;
#[doc = " Invalid / sentinel value."]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_INVALID:
    GhosttyKittyGraphicsPlacementData = 0;
#[doc = " The image ID this placement belongs to.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID:
    GhosttyKittyGraphicsPlacementData = 1;
#[doc = " The placement ID.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_PLACEMENT_ID:
    GhosttyKittyGraphicsPlacementData = 2;
#[doc = " Whether this is a virtual placement (unicode placeholder).\n\n Output type: bool *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IS_VIRTUAL:
    GhosttyKittyGraphicsPlacementData = 3;
#[doc = " Pixel offset from the left edge of the cell.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET:
    GhosttyKittyGraphicsPlacementData = 4;
#[doc = " Pixel offset from the top edge of the cell.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET:
    GhosttyKittyGraphicsPlacementData = 5;
#[doc = " Source rectangle x origin in pixels.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_SOURCE_X:
    GhosttyKittyGraphicsPlacementData = 6;
#[doc = " Source rectangle y origin in pixels.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_SOURCE_Y:
    GhosttyKittyGraphicsPlacementData = 7;
#[doc = " Source rectangle width in pixels (0 = full image width).\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_SOURCE_WIDTH:
    GhosttyKittyGraphicsPlacementData = 8;
#[doc = " Source rectangle height in pixels (0 = full image height).\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_SOURCE_HEIGHT:
    GhosttyKittyGraphicsPlacementData = 9;
#[doc = " Number of columns this placement occupies.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_COLUMNS:
    GhosttyKittyGraphicsPlacementData = 10;
#[doc = " Number of rows this placement occupies.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_ROWS:
    GhosttyKittyGraphicsPlacementData = 11;
#[doc = " Z-index for this placement.\n\n Output type: int32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z:
    GhosttyKittyGraphicsPlacementData = 12;
#[doc = " Z-index for this placement.\n\n Output type: int32_t *"]
pub const GhosttyKittyGraphicsPlacementData_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_MAX_VALUE:
    GhosttyKittyGraphicsPlacementData = 2147483647;
#[doc = " Queryable data kinds for ghostty_kitty_graphics_placement_get().\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyGraphicsPlacementData = ::std::os::raw::c_uint;
pub const GhosttyKittyPlacementLayer_GHOSTTY_KITTY_PLACEMENT_LAYER_ALL: GhosttyKittyPlacementLayer =
    0;
pub const GhosttyKittyPlacementLayer_GHOSTTY_KITTY_PLACEMENT_LAYER_BELOW_BG:
    GhosttyKittyPlacementLayer = 1;
pub const GhosttyKittyPlacementLayer_GHOSTTY_KITTY_PLACEMENT_LAYER_BELOW_TEXT:
    GhosttyKittyPlacementLayer = 2;
pub const GhosttyKittyPlacementLayer_GHOSTTY_KITTY_PLACEMENT_LAYER_ABOVE_TEXT:
    GhosttyKittyPlacementLayer = 3;
pub const GhosttyKittyPlacementLayer_GHOSTTY_KITTY_PLACEMENT_LAYER_MAX_VALUE:
    GhosttyKittyPlacementLayer = 2147483647;
#[doc = " Z-layer classification for kitty graphics placements.\n\n Based on the kitty protocol z-index conventions:\n - BELOW_BG:   z < INT32_MIN/2  (drawn below cell background)\n - BELOW_TEXT:  INT32_MIN/2 <= z < 0  (above background, below text)\n - ABOVE_TEXT:  z >= 0  (above text)\n - ALL:         no filtering (current behavior)\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyPlacementLayer = ::std::os::raw::c_uint;
#[doc = " Set the z-layer filter for the iterator.\n\n Input type: GhosttyKittyPlacementLayer *"]
pub const GhosttyKittyGraphicsPlacementIteratorOption_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_ITERATOR_OPTION_LAYER : GhosttyKittyGraphicsPlacementIteratorOption = 0 ;
#[doc = " Set the z-layer filter for the iterator.\n\n Input type: GhosttyKittyPlacementLayer *"]
pub const GhosttyKittyGraphicsPlacementIteratorOption_GHOSTTY_KITTY_GRAPHICS_PLACEMENT_ITERATOR_OPTION_MAX_VALUE : GhosttyKittyGraphicsPlacementIteratorOption = 2147483647 ;
#[doc = " Settable options for ghostty_kitty_graphics_placement_iterator_set().\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyGraphicsPlacementIteratorOption = ::std::os::raw::c_uint;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_RGB: GhosttyKittyImageFormat = 0;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_RGBA: GhosttyKittyImageFormat = 1;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_PNG: GhosttyKittyImageFormat = 2;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_GRAY_ALPHA: GhosttyKittyImageFormat =
    3;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_GRAY: GhosttyKittyImageFormat = 4;
pub const GhosttyKittyImageFormat_GHOSTTY_KITTY_IMAGE_FORMAT_MAX_VALUE: GhosttyKittyImageFormat =
    2147483647;
#[doc = " Pixel format of a Kitty graphics image.\n\n Note that stored images are always fully decoded:\n GHOSTTY_KITTY_IMAGE_FORMAT_PNG is never returned by\n ghostty_kitty_graphics_image_get() because PNG payloads are decoded\n to GHOSTTY_KITTY_IMAGE_FORMAT_RGBA before storage. The PNG value\n exists only for protocol-level completeness.\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyImageFormat = ::std::os::raw::c_uint;
pub const GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE:
    GhosttyKittyImageCompression = 0;
pub const GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_ZLIB_DEFLATE:
    GhosttyKittyImageCompression = 1;
pub const GhosttyKittyImageCompression_GHOSTTY_KITTY_IMAGE_COMPRESSION_MAX_VALUE:
    GhosttyKittyImageCompression = 2147483647;
#[doc = " Compression of a Kitty graphics image.\n\n Note that stored images are always decompressed:\n GHOSTTY_KITTY_IMAGE_COMPRESSION_ZLIB_DEFLATE payloads are inflated\n before storage, so ghostty_kitty_graphics_image_get() always reports\n GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE. Consumers never need to\n inflate image data themselves.\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyImageCompression = ::std::os::raw::c_uint;
#[doc = " Invalid / sentinel value."]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_INVALID:
    GhosttyKittyGraphicsImageData = 0;
#[doc = " The image ID.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_ID: GhosttyKittyGraphicsImageData =
    1;
#[doc = " The image number.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_NUMBER:
    GhosttyKittyGraphicsImageData = 2;
#[doc = " Image width in pixels.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_WIDTH:
    GhosttyKittyGraphicsImageData = 3;
#[doc = " Image height in pixels.\n\n Output type: uint32_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_HEIGHT:
    GhosttyKittyGraphicsImageData = 4;
#[doc = " Pixel format of the image. Never GHOSTTY_KITTY_IMAGE_FORMAT_PNG;\n PNG payloads are decoded to RGBA before storage.\n\n Output type: GhosttyKittyImageFormat *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_FORMAT:
    GhosttyKittyGraphicsImageData = 5;
#[doc = " Compression of the image. Always\n GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE; compressed payloads are\n inflated before storage.\n\n Output type: GhosttyKittyImageCompression *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_COMPRESSION:
    GhosttyKittyGraphicsImageData = 6;
#[doc = " Borrowed pointer to the raw pixel data. Valid as long as the\n underlying terminal is not mutated.\n\n The data is always fully decoded, uncompressed pixels in the\n format reported by GHOSTTY_KITTY_IMAGE_DATA_FORMAT: zlib payloads\n are inflated and PNG payloads are decoded to RGBA at transmission\n time, before the image is stored. Consumers can upload this\n directly to the GPU without any decode step.\n\n Output type: const uint8_t **"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_DATA_PTR:
    GhosttyKittyGraphicsImageData = 7;
#[doc = " Length of the raw pixel data in bytes. Always equal to\n width * height * bytes-per-pixel for the reported format.\n\n Output type: size_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_DATA_LEN:
    GhosttyKittyGraphicsImageData = 8;
#[doc = " Generation stamp assigned when this image was added to (or\n replaced in) the storage. A changed generation for a given image\n ID means the pixel contents may have changed even when the\n dimensions, format, and data length are identical (e.g. a\n retransmission of the same image ID), so texture caches must key\n staleness on this value rather than on size heuristics.\n\n Stamps are unique and monotonically increasing process-wide and\n are drawn from the same sequence as\n GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION. Never zero for a stored\n image, so zero can be used as an \"empty\" sentinel by callers.\n\n Output type: uint64_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_GENERATION:
    GhosttyKittyGraphicsImageData = 9;
#[doc = " Generation stamp assigned when this image was added to (or\n replaced in) the storage. A changed generation for a given image\n ID means the pixel contents may have changed even when the\n dimensions, format, and data length are identical (e.g. a\n retransmission of the same image ID), so texture caches must key\n staleness on this value rather than on size heuristics.\n\n Stamps are unique and monotonically increasing process-wide and\n are drawn from the same sequence as\n GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION. Never zero for a stored\n image, so zero can be used as an \"empty\" sentinel by callers.\n\n Output type: uint64_t *"]
pub const GhosttyKittyGraphicsImageData_GHOSTTY_KITTY_IMAGE_DATA_MAX_VALUE:
    GhosttyKittyGraphicsImageData = 2147483647;
#[doc = " Queryable data kinds for ghostty_kitty_graphics_image_get().\n\n @ingroup kitty_graphics"]
pub type GhosttyKittyGraphicsImageData = ::std::os::raw::c_uint;
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
#[doc = " Terminal initialization options.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyTerminalOptions {
    #[doc = " Terminal width in cells. Must be greater than zero."]
    pub cols: u16,
    #[doc = " Terminal height in cells. Must be greater than zero."]
    pub rows: u16,
    #[doc = " Maximum number of lines to keep in scrollback history."]
    pub max_scrollback: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalOptions"][::std::mem::size_of::<GhosttyTerminalOptions>() - 16usize];
    ["Alignment of GhosttyTerminalOptions"]
        [::std::mem::align_of::<GhosttyTerminalOptions>() - 8usize];
    ["Offset of field: GhosttyTerminalOptions::cols"]
        [::std::mem::offset_of!(GhosttyTerminalOptions, cols) - 0usize];
    ["Offset of field: GhosttyTerminalOptions::rows"]
        [::std::mem::offset_of!(GhosttyTerminalOptions, rows) - 2usize];
    ["Offset of field: GhosttyTerminalOptions::max_scrollback"]
        [::std::mem::offset_of!(GhosttyTerminalOptions, max_scrollback) - 8usize];
};
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
pub type GhosttyTerminalCompressionMode = ::std::os::raw::c_uint;
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
pub type GhosttyTerminalCompressionResult = ::std::os::raw::c_uint;
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
pub type GhosttyTerminalScrollViewportTag = ::std::os::raw::c_uint;
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
pub type GhosttyTerminalScreen = ::std::os::raw::c_uint;
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
pub type GhosttyTerminalCursorStyle = ::std::os::raw::c_uint;
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
pub type GhosttyClipboardLocation = ::std::os::raw::c_uint;
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
#[doc = " A semantic, atomic clipboard write.\n\n This is a sized struct. The callback must only access fields present in the\n size reported by `size`. The request, contents array, MIME strings, and\n data strings are all borrowed and valid only for the callback duration.\n\n All entries in `contents` are representations of the same logical value\n and must be committed atomically. A `contents_len` of zero requests that\n the destination be cleared. This is distinct from a content entry whose data\n has zero length.\n\n @ingroup terminal"]
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
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardWrite"][::std::mem::size_of::<GhosttyClipboardWrite>() - 32usize];
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
#[doc = " Result of a clipboard write callback.\n\n Protocols without write acknowledgements, including OSC 52 and iTerm2\n OSC 1337 Copy, ignore this result.\n\n @ingroup terminal"]
pub type GhosttyClipboardWriteResult = ::std::os::raw::c_uint;
#[doc = " Callback function type for clipboard_write.\n\n Called synchronously for a complete logical clipboard write. Protocol\n details such as OSC 52 selectors, base64 encoding, multipart chunks,\n aliases, and terminators are normalized before this callback is invoked.\n OSC 52 and iTerm2 OSC 1337 Copy writes therefore use the same callback\n shape. OSC 52 clipboard read requests (\"?\") are always ignored and never\n forwarded to this callback.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param write Borrowed atomic clipboard write request\n @return The result of attempting the clipboard write\n\n @ingroup terminal"]
pub type GhosttyTerminalClipboardWriteFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        write: *const GhosttyClipboardWrite,
    ) -> GhosttyClipboardWriteResult,
>;
#[doc = " Callback function type for color scheme queries (CSI ? 996 n).\n\n Called when the terminal receives a color scheme device status report\n query. Return true and fill *out_scheme with the current color scheme,\n or return false to silently ignore the query.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param[out] out_scheme Pointer to store the current color scheme\n @return true if the color scheme was filled, false to ignore the query\n\n @ingroup terminal"]
pub type GhosttyTerminalColorSchemeFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        out_scheme: *mut GhosttyColorScheme,
    ) -> bool,
>;
#[doc = " Callback function type for device attributes queries (DA1/DA2/DA3).\n\n Called when the terminal receives a device attributes query (CSI c,\n CSI > c, or CSI = c). Return true and fill *out_attrs with the\n response data, or return false to silently ignore the query.\n\n The terminal uses whichever sub-struct (primary, secondary, tertiary)\n matches the request type, but all three should be filled for simplicity.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param[out] out_attrs Pointer to store the device attributes response\n @return true if attributes were filled, false to ignore the query\n\n @ingroup terminal"]
pub type GhosttyTerminalDeviceAttributesFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        out_attrs: *mut GhosttyDeviceAttributes,
    ) -> bool,
>;
#[doc = " Callback function type for enquiry (ENQ, 0x05).\n\n Called when the terminal receives an ENQ character. Return the\n response bytes as a GhosttyString. The memory must remain valid\n until the callback returns. Return a zero-length string to send\n no response.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @return The response bytes to write back to the pty\n\n @ingroup terminal"]
pub type GhosttyTerminalEnquiryFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
    ) -> GhosttyString,
>;
#[doc = " Callback function type for size queries (XTWINOPS).\n\n Called in response to XTWINOPS size queries (CSI 14/16/18 t).\n Return true and fill *out_size with the current terminal geometry,\n or return false to silently ignore the query.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param[out] out_size Pointer to store the terminal size information\n @return true if size was filled, false to ignore the query\n\n @ingroup terminal"]
pub type GhosttyTerminalSizeFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        out_size: *mut GhosttySizeReportSize,
    ) -> bool,
>;
#[doc = " Callback function type for title_changed.\n\n Called when the terminal title changes via escape sequences\n (e.g. OSC 0 or OSC 2). The new title can be queried from the\n terminal after the callback returns.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n\n @ingroup terminal"]
pub type GhosttyTerminalTitleChangedFn = ::std::option::Option<
    unsafe extern "C" fn(terminal: GhosttyTerminal, userdata: *mut ::std::os::raw::c_void),
>;
#[doc = " Callback function type for pwd_changed.\n\n Called when the terminal pwd (current working directory) changes via\n escape sequences: OSC 7 (file:// URI), OSC 9 (ConEmu CurrentDir), or\n OSC 1337 CurrentDir (iTerm2). Use ghostty_terminal_get() with\n GHOSTTY_TERMINAL_DATA_PWD inside the callback to read the new value.\n\n The terminal stores whatever bytes the shell emitted, without parsing.\n That means for OSC 7 the value is the raw URI (typically file://...);\n for OSC 9/OSC 1337 it is typically a bare path. The embedder is\n responsible for decoding any URI scheme or host if it cares about them.\n\n The callback also fires when the shell clears the pwd (e.g. an empty\n OSC 7). In that case GHOSTTY_TERMINAL_DATA_PWD returns a zero-length\n string.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n\n @ingroup terminal"]
pub type GhosttyTerminalPwdChangedFn = ::std::option::Option<
    unsafe extern "C" fn(terminal: GhosttyTerminal, userdata: *mut ::std::os::raw::c_void),
>;
#[doc = " Callback function type for write_pty.\n\n Called when the terminal needs to write data back to the pty, for\n example in response to a device status report or mode query. The\n data is only valid for the duration of the call; callers must copy\n it if it needs to persist.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param data Pointer to the response bytes\n @param len Length of the response in bytes\n\n @ingroup terminal"]
pub type GhosttyTerminalWritePtyFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        data: *const u8,
        len: usize,
    ),
>;
#[doc = " Callback function type for XTVERSION.\n\n Called when the terminal receives an XTVERSION query (CSI > q).\n Return the version string (e.g. \"myterm 1.0\") as a GhosttyString.\n The memory must remain valid until the callback returns. Return a\n zero-length string to report the default \"libghostty\" version.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @return The version string to report\n\n @ingroup terminal"]
pub type GhosttyTerminalXtversionFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
    ) -> GhosttyString,
>;
#[doc = " Opaque userdata pointer passed to all callbacks.\n\n Input type: void*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_USERDATA: GhosttyTerminalOption = 0;
#[doc = " Callback invoked when the terminal needs to write data back\n to the pty (e.g. in response to a DECRQM query or device\n status report). Set to NULL to ignore such sequences.\n\n Input type: GhosttyTerminalWritePtyFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_WRITE_PTY: GhosttyTerminalOption = 1;
#[doc = " Callback invoked when the terminal receives a BEL character\n (0x07). Set to NULL to ignore bell events.\n\n Input type: GhosttyTerminalBellFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_BELL: GhosttyTerminalOption = 2;
#[doc = " Callback invoked when the terminal receives an ENQ character\n (0x05). Set to NULL to send no response.\n\n Input type: GhosttyTerminalEnquiryFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_ENQUIRY: GhosttyTerminalOption = 3;
#[doc = " Callback invoked when the terminal receives an XTVERSION query\n (CSI > q). Set to NULL to report the default \"libghostty\" string.\n\n Input type: GhosttyTerminalXtversionFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_XTVERSION: GhosttyTerminalOption = 4;
#[doc = " Callback invoked when the terminal title changes via escape\n sequences (e.g. OSC 0 or OSC 2). Set to NULL to ignore title\n change events.\n\n Input type: GhosttyTerminalTitleChangedFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_TITLE_CHANGED: GhosttyTerminalOption = 5;
#[doc = " Callback invoked in response to XTWINOPS size queries\n (CSI 14/16/18 t). Set to NULL to silently ignore size queries.\n\n Input type: GhosttyTerminalSizeFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_SIZE: GhosttyTerminalOption = 6;
#[doc = " Callback invoked in response to a color scheme device status\n report query (CSI ? 996 n). Return true and fill the out pointer\n to report the current scheme, or return false to silently ignore.\n Set to NULL to ignore color scheme queries.\n\n Input type: GhosttyTerminalColorSchemeFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_SCHEME: GhosttyTerminalOption = 7;
#[doc = " Callback invoked in response to a device attributes query\n (CSI c, CSI > c, or CSI = c). Return true and fill the out\n pointer with response data, or return false to silently ignore.\n Set to NULL to ignore device attributes queries.\n\n Input type: GhosttyTerminalDeviceAttributesFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_DEVICE_ATTRIBUTES: GhosttyTerminalOption = 8;
#[doc = " Set the terminal title manually.\n\n The string data is copied into the terminal. A NULL value pointer\n clears the title (equivalent to setting an empty string).\n\n Input type: GhosttyString*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_TITLE: GhosttyTerminalOption = 9;
#[doc = " Set the terminal working directory manually.\n\n The string data is copied into the terminal. A NULL value pointer\n clears the pwd (equivalent to setting an empty string).\n\n Input type: GhosttyString*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_PWD: GhosttyTerminalOption = 10;
#[doc = " Set the default foreground color.\n\n A NULL value pointer clears the default (unset).\n\n Input type: GhosttyColorRgb*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND: GhosttyTerminalOption = 11;
#[doc = " Set the default background color.\n\n A NULL value pointer clears the default (unset).\n\n Input type: GhosttyColorRgb*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND: GhosttyTerminalOption = 12;
#[doc = " Set the default cursor color.\n\n A NULL value pointer clears the default (unset).\n\n Input type: GhosttyColorRgb*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_CURSOR: GhosttyTerminalOption = 13;
#[doc = " Set the default 256-color palette.\n\n The value must point to an array of exactly 256 GhosttyColorRgb values.\n A NULL value pointer resets to the built-in default palette.\n\n Input type: GhosttyColorRgb[256]*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_COLOR_PALETTE: GhosttyTerminalOption = 14;
#[doc = " Set the Kitty image storage limit in bytes.\n\n Applied to all initialized screens (primary and alternate).\n A value of zero disables the Kitty graphics protocol entirely,\n deleting all stored images and placements. A NULL value pointer\n is equivalent to zero (disables). Has no effect when Kitty graphics\n are disabled at build time.\n\n Input type: uint64_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT:
    GhosttyTerminalOption = 15;
#[doc = " Enable or disable Kitty image loading via the file medium.\n\n A NULL value pointer is a no-op. Has no effect when Kitty graphics\n are disabled at build time.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_FILE:
    GhosttyTerminalOption = 16;
#[doc = " Enable or disable Kitty image loading via the temporary file medium.\n\n A NULL value pointer is a no-op. Has no effect when Kitty graphics\n are disabled at build time.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_TEMP_FILE:
    GhosttyTerminalOption = 17;
#[doc = " Enable or disable Kitty image loading via the shared memory medium.\n\n A NULL value pointer is a no-op. Has no effect when Kitty graphics\n are disabled at build time.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_SHARED_MEM:
    GhosttyTerminalOption = 18;
#[doc = " Set the maximum bytes the APC handler will buffer for all protocols.\n This prevents malicious input from causing unbounded memory allocation.\n A NULL value pointer removes all overrides, reverting to the built-in\n defaults.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_APC_MAX_BYTES: GhosttyTerminalOption = 19;
#[doc = " Set the maximum bytes the APC handler will buffer for Kitty graphics\n protocol data. A NULL value pointer removes the override, reverting\n to the built-in default.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_APC_MAX_BYTES_KITTY: GhosttyTerminalOption =
    20;
#[doc = " Set the active screen selection.\n\n The value must point to a GhosttySelection whose grid references are\n valid for this terminal's active screen at the time of the call. The\n terminal copies the selection immediately and converts it to\n terminal-owned tracked state, so the GhosttySelection struct and its\n untracked grid references do not need to outlive this call.\n\n Passing NULL clears the active screen selection.\n\n Input type: GhosttySelection*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_SELECTION: GhosttyTerminalOption = 21;
#[doc = " Set the default cursor style used by DECSCUSR reset (CSI 0 q).\n\n A NULL value pointer resets to the built-in default block cursor.\n\n Input type: GhosttyTerminalCursorStyle*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_STYLE: GhosttyTerminalOption =
    22;
#[doc = " Set whether the default cursor should blink when reset by DECSCUSR\n (CSI 0 q).\n\n A NULL value pointer resets to the built-in default of not blinking.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_BLINK: GhosttyTerminalOption =
    23;
#[doc = " Enable or disable Glyph Protocol APC handling.\n\n When disabled, Glyph Protocol APC sequences are ignored and no\n support/query/register/clear responses are emitted. Disabling also clears\n the terminal session's glyph glossary. A NULL value pointer is a no-op.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_GLYPH_PROTOCOL: GhosttyTerminalOption = 24;
#[doc = " Callback invoked when the terminal pwd changes via escape\n sequences (OSC 7, OSC 9, or OSC 1337 CurrentDir). Set to NULL\n to ignore pwd change events.\n\n Input type: GhosttyTerminalPwdChangedFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_PWD_CHANGED: GhosttyTerminalOption = 25;
#[doc = " Callback invoked when the running program performs a clipboard write.\n OSC 52 and iTerm2 OSC 1337 Copy writes are normalized to an atomic set\n of decoded MIME representations. Set to NULL to ignore clipboard writes.\n Clipboard read requests are always ignored; see\n GhosttyTerminalClipboardWriteFn.\n\n Input type: GhosttyTerminalClipboardWriteFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE: GhosttyTerminalOption = 26;
#[doc = " Callback invoked when the running program performs a clipboard write.\n OSC 52 and iTerm2 OSC 1337 Copy writes are normalized to an atomic set\n of decoded MIME representations. Set to NULL to ignore clipboard writes.\n Clipboard read requests are always ignored; see\n GhosttyTerminalClipboardWriteFn.\n\n Input type: GhosttyTerminalClipboardWriteFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_MAX_VALUE: GhosttyTerminalOption = 2147483647;
#[doc = " Terminal option identifiers.\n\n These values are used with ghostty_terminal_set() to configure\n terminal callbacks and associated state.\n\n @ingroup terminal"]
pub type GhosttyTerminalOption = ::std::os::raw::c_uint;
#[doc = " Invalid data type. Never results in any data extraction."]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_INVALID: GhosttyTerminalData = 0;
#[doc = " Terminal width in cells.\n\n Output type: uint16_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLS: GhosttyTerminalData = 1;
#[doc = " Terminal height in cells.\n\n Output type: uint16_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_ROWS: GhosttyTerminalData = 2;
#[doc = " Cursor column position (0-indexed).\n\n Output type: uint16_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_X: GhosttyTerminalData = 3;
#[doc = " Cursor row position within the active area (0-indexed).\n\n Output type: uint16_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_Y: GhosttyTerminalData = 4;
#[doc = " Whether the cursor has a pending wrap (next print will soft-wrap).\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_PENDING_WRAP: GhosttyTerminalData = 5;
#[doc = " The currently active screen.\n\n Output type: GhosttyTerminalScreen *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN: GhosttyTerminalData = 6;
#[doc = " Whether the cursor is visible (DEC mode 25).\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_VISIBLE: GhosttyTerminalData = 7;
#[doc = " Current Kitty keyboard protocol flags.\n\n Output type: GhosttyKittyKeyFlags * (uint8_t *)"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_KEYBOARD_FLAGS: GhosttyTerminalData = 8;
#[doc = " Scrollbar state for the terminal viewport.\n\n This is amortized O(1): the total is maintained incrementally as\n the terminal is modified and the viewport offset is cached. The\n first read after the viewport moves to an arbitrary position that\n isn't an absolute row (e.g. scrolling to a selection) may cost\n O(pages) to compute the offset, after which it is cached again.\n\n There is intentionally no change notification for scroll state.\n Callers building scrollbars should poll this once per frame or\n per write batch and diff the result to detect changes; this is\n what Ghostty's own renderer does.\n\n Output type: GhosttyTerminalScrollbar *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBAR: GhosttyTerminalData = 9;
#[doc = " The current SGR style of the cursor.\n\n This is the style that will be applied to newly printed characters.\n\n Output type: GhosttyStyle *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_STYLE: GhosttyTerminalData = 10;
#[doc = " Whether any mouse tracking mode is active.\n\n Returns true if any of the mouse tracking modes (X10, normal, button,\n or any-event) are enabled.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING: GhosttyTerminalData = 11;
#[doc = " The terminal title as set by escape sequences (e.g. OSC 0/2).\n\n Returns a borrowed string. The pointer is valid until the next call\n to ghostty_terminal_vt_write() or ghostty_terminal_reset(). An empty\n string (len=0) is returned when no title has been set.\n\n Output type: GhosttyString *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_TITLE: GhosttyTerminalData = 12;
#[doc = " The terminal's current working directory as set by escape sequences\n (e.g. OSC 7).\n\n Returns a borrowed string. The pointer is valid until the next call\n to ghostty_terminal_vt_write() or ghostty_terminal_reset(). An empty\n string (len=0) is returned when no pwd has been set.\n\n Output type: GhosttyString *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_PWD: GhosttyTerminalData = 13;
#[doc = " The total number of rows in the active screen including scrollback.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_TOTAL_ROWS: GhosttyTerminalData = 14;
#[doc = " The number of scrollback rows (total rows minus viewport rows).\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBACK_ROWS: GhosttyTerminalData = 15;
#[doc = " The total width of the terminal in pixels.\n\n This is cols * cell_width_px as set by ghostty_terminal_resize().\n\n Output type: uint32_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_WIDTH_PX: GhosttyTerminalData = 16;
#[doc = " The total height of the terminal in pixels.\n\n This is rows * cell_height_px as set by ghostty_terminal_resize().\n\n Output type: uint32_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_HEIGHT_PX: GhosttyTerminalData = 17;
#[doc = " The effective foreground color (override or default).\n\n Returns GHOSTTY_NO_VALUE if no foreground color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_FOREGROUND: GhosttyTerminalData = 18;
#[doc = " The effective background color (override or default).\n\n Returns GHOSTTY_NO_VALUE if no background color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND: GhosttyTerminalData = 19;
#[doc = " The effective cursor color (override or default).\n\n Returns GHOSTTY_NO_VALUE if no cursor color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_CURSOR: GhosttyTerminalData = 20;
#[doc = " The current 256-color palette.\n\n Output type: GhosttyColorRgb[256] *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_PALETTE: GhosttyTerminalData = 21;
#[doc = " The default foreground color (ignoring any OSC override).\n\n Returns GHOSTTY_NO_VALUE if no default foreground color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_FOREGROUND_DEFAULT: GhosttyTerminalData =
    22;
#[doc = " The default background color (ignoring any OSC override).\n\n Returns GHOSTTY_NO_VALUE if no default background color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND_DEFAULT: GhosttyTerminalData =
    23;
#[doc = " The default cursor color (ignoring any OSC override).\n\n Returns GHOSTTY_NO_VALUE if no default cursor color is set.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_CURSOR_DEFAULT: GhosttyTerminalData = 24;
#[doc = " The default 256-color palette (ignoring any OSC overrides).\n\n Output type: GhosttyColorRgb[256] *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_COLOR_PALETTE_DEFAULT: GhosttyTerminalData = 25;
#[doc = " The Kitty image storage limit in bytes for the active screen.\n\n A value of zero means the Kitty graphics protocol is disabled.\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: uint64_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_STORAGE_LIMIT: GhosttyTerminalData =
    26;
#[doc = " Whether the file medium is enabled for Kitty image loading on the\n active screen.\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_FILE: GhosttyTerminalData =
    27;
#[doc = " Whether the temporary file medium is enabled for Kitty image loading\n on the active screen.\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_TEMP_FILE:
    GhosttyTerminalData = 28;
#[doc = " Whether the shared memory medium is enabled for Kitty image loading\n on the active screen.\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_SHARED_MEM:
    GhosttyTerminalData = 29;
#[doc = " The Kitty graphics image storage for the active screen.\n\n Returns a borrowed pointer to the image storage. The pointer is valid\n until the next mutating terminal call (e.g. ghostty_terminal_vt_write()\n or ghostty_terminal_reset()).\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: GhosttyKittyGraphics *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS: GhosttyTerminalData = 30;
#[doc = " The active screen's current selection.\n\n On success, writes an untracked snapshot of the terminal-owned selection\n to the caller-provided GhosttySelection. The GhosttySelection struct is\n caller-owned and may be kept, but the grid references inside it are\n untracked borrowed references into the active screen. They are only valid\n until the next mutating terminal call, such as ghostty_terminal_set(),\n ghostty_terminal_vt_write(), ghostty_terminal_resize(), or\n ghostty_terminal_reset().\n\n Returns GHOSTTY_NO_VALUE when there is no active selection.\n\n Output type: GhosttySelection *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SELECTION: GhosttyTerminalData = 31;
#[doc = " Whether the viewport is currently pinned to the active area.\n\n This is true when the viewport is following the active terminal area,\n and false when the user has scrolled into history.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_VIEWPORT_ACTIVE: GhosttyTerminalData = 32;
#[doc = " Whether the viewport is currently pinned to the active area.\n\n This is true when the viewport is following the active terminal area,\n and false when the user has scrolled into history.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_MAX_VALUE: GhosttyTerminalData = 2147483647;
#[doc = " Terminal data types.\n\n These values specify what type of data to extract from a terminal\n using `ghostty_terminal_get`.\n\n @ingroup terminal"]
pub type GhosttyTerminalData = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Create a new terminal instance.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param terminal Pointer to store the created terminal handle\n @param options Terminal initialization options\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup terminal"]
    pub fn ghostty_terminal_new(
        allocator: *const GhosttyAllocator,
        terminal: *mut GhosttyTerminal,
        options: GhosttyTerminalOptions,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a terminal instance.\n\n Releases all resources associated with the terminal. After this call,\n the terminal handle becomes invalid and must not be used.\n\n @param terminal The terminal handle to free (may be NULL)\n\n @ingroup terminal"]
    pub fn ghostty_terminal_free(terminal: GhosttyTerminal);
}
unsafe extern "C" {
    #[doc = " Perform a full reset of the terminal (RIS).\n\n Resets all terminal state back to its initial configuration, including\n modes, scrollback, scrolling region, and screen contents. The terminal\n dimensions are preserved.\n\n @param terminal The terminal handle (may be NULL, in which case this is a no-op)\n\n @ingroup terminal"]
    pub fn ghostty_terminal_reset(terminal: GhosttyTerminal);
}
unsafe extern "C" {
    #[doc = " Resize the terminal to the given dimensions.\n\n Changes the number of columns and rows in the terminal. The primary\n screen will reflow content if wraparound mode is enabled; the alternate\n screen does not reflow. If the dimensions are unchanged, this is a no-op.\n\n This also updates the terminal's pixel dimensions (used for image\n protocols and size reports), disables synchronized output mode (allowed\n by the spec so that resize results are shown immediately), and sends an\n in-band size report if mode 2048 is enabled.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param cols New width in cells (must be greater than zero)\n @param rows New height in cells (must be greater than zero)\n @param cell_width_px Width of a single cell in pixels\n @param cell_height_px Height of a single cell in pixels\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup terminal"]
    pub fn ghostty_terminal_resize(
        terminal: GhosttyTerminal,
        cols: u16,
        rows: u16,
        cell_width_px: u32,
        cell_height_px: u32,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Set an option on the terminal.\n\n Configures terminal callbacks and associated state such as the\n write_pty callback and userdata pointer. The value is passed\n directly for pointer types (callbacks, userdata) or as a pointer\n to the value for non-pointer types (e.g. GhosttyString*).\n NULL clears the option to its default.\n\n Callbacks are invoked synchronously during ghostty_terminal_vt_write().\n Callbacks must not call ghostty_terminal_vt_write() on the same\n terminal (no reentrancy).\n\n @param terminal The terminal handle (may be NULL, in which case this is a no-op)\n @param option The option to set\n @param value Pointer to the value to set (type depends on the option),\n              or NULL to clear the option\n\n @ingroup terminal"]
    pub fn ghostty_terminal_set(
        terminal: GhosttyTerminal,
        option: GhosttyTerminalOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Write VT-encoded data to the terminal for processing.\n\n Feeds raw bytes through the terminal's VT stream parser, updating\n terminal state accordingly. By default, sequences that require output\n (queries, device status reports) are silently ignored. Use\n ghostty_terminal_set() with GHOSTTY_TERMINAL_OPT_WRITE_PTY to install\n a callback that receives response data.\n\n This never fails. Any erroneous input or errors in processing the\n input are logged internally but do not cause this function to fail\n because this input is assumed to be untrusted and from an external\n source; so the primary goal is to keep the terminal state consistent and\n not allow malformed input to corrupt or crash.\n\n @param terminal The terminal handle\n @param data Pointer to the data to write\n @param len Length of the data in bytes\n\n @ingroup terminal"]
    pub fn ghostty_terminal_vt_write(terminal: GhosttyTerminal, data: *const u8, len: usize);
}
unsafe extern "C" {
    #[doc = " Scroll the terminal viewport.\n\n Scrolls the terminal's viewport according to the given behavior.\n When using GHOSTTY_SCROLL_VIEWPORT_DELTA, set the delta field in\n the value union to specify the number of rows to scroll (negative\n for up, positive for down). When using GHOSTTY_SCROLL_VIEWPORT_ROW,\n set the row field to the absolute row offset from the top of the\n scrollable area (the same row space as the offset field of\n GhosttyTerminalScrollbar). For other behaviors, the value is ignored.\n\n @param terminal The terminal handle (may be NULL, in which case this is a no-op)\n @param behavior The scroll behavior as a tagged union\n\n @ingroup terminal"]
    pub fn ghostty_terminal_scroll_viewport(
        terminal: GhosttyTerminal,
        behavior: GhosttyTerminalScrollViewport,
    );
}
