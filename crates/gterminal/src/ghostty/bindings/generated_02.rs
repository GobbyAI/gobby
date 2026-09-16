unsafe extern "C" {
    #[doc = " Encode a focus event into a terminal escape sequence.\n\n Encodes a focus gained (CSI I) or focus lost (CSI O) report into the\n provided buffer.\n\n If the buffer is too small, the function returns GHOSTTY_OUT_OF_SPACE\n and writes the required buffer size to @p out_written. The caller can\n then retry with a sufficiently sized buffer.\n\n @param event The focus event to encode\n @param buf Output buffer to write the encoded sequence into (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_written On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if the buffer\n         is too small"]
    pub fn ghostty_focus_encode(
        event: GhosttyFocusEvent,
        buf: *mut ::std::os::raw::c_char,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " Read bytes from a source.\n\n The callback must set @p out_read to a value no greater than @p capacity\n when returning true. A positive value reports progress; it may be less than\n capacity and does not indicate end-of-file. A zero value is definitive\n end-of-file. It must not be used to report temporary input starvation or a\n would-block condition.\n\n Returning false reports a fatal read error and the value of @p out_read is\n ignored. The library does not inspect or modify errno.\n\n All pointer arguments are borrowed and valid only for the duration of the\n callback. The callback is invoked synchronously on the calling thread.\n\n @param userdata Opaque userdata from GhosttyReader\n @param buffer Destination for read bytes; always non-NULL\n @param capacity Writable capacity of @p buffer; always greater than zero\n @param[out] out_read Number of bytes read when returning true; non-NULL\n @return true for a successful read or end-of-file, false for a fatal error"]
pub type GhosttyReaderFn = ::std::option::Option<
    unsafe extern "C" fn(
        userdata: *mut ::std::os::raw::c_void,
        buffer: *mut u8,
        capacity: usize,
        out_read: *mut usize,
    ) -> bool,
>;
#[doc = " Write bytes to a destination.\n\n Returning true means all @p len bytes were accepted. Returning false\n reports a fatal write error. A callback wrapping an interface that permits\n partial writes must retry internally until the full slice is accepted or\n an error occurs.\n\n On failure, the destination may already contain a prefix of the bytes. The\n calling operation fails and must not be resumed from that partial output.\n The library does not inspect or modify errno.\n\n @p data is borrowed and valid only for the duration of the callback. The\n callback is invoked synchronously on the calling thread. Successful return\n means the bytes were handed to the destination; it does not imply that the\n destination was flushed or made durable.\n\n @param userdata Opaque userdata from GhosttyWriter\n @param data Source bytes; always non-NULL\n @param len Number of source bytes; always greater than zero\n @return true if the complete slice was accepted, false on fatal error"]
pub type GhosttyWriterFn = ::std::option::Option<
    unsafe extern "C" fn(
        userdata: *mut ::std::os::raw::c_void,
        data: *const u8,
        len: usize,
    ) -> bool,
>;
#[doc = " A byte source callback and its opaque context.\n\n The struct is passed by value. @p read must be non-NULL."]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyReader {
    pub read: GhosttyReaderFn,
    pub userdata: *mut ::std::os::raw::c_void,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyReader"][::std::mem::size_of::<GhosttyReader>() - 16usize];
    ["Alignment of GhosttyReader"][::std::mem::align_of::<GhosttyReader>() - 8usize];
    ["Offset of field: GhosttyReader::read"][::std::mem::offset_of!(GhosttyReader, read) - 0usize];
    ["Offset of field: GhosttyReader::userdata"]
        [::std::mem::offset_of!(GhosttyReader, userdata) - 8usize];
};
impl Default for GhosttyReader {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " A byte destination callback and its opaque context.\n\n The struct is passed by value. @p write must be non-NULL."]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyWriter {
    pub write: GhosttyWriterFn,
    pub userdata: *mut ::std::os::raw::c_void,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyWriter"][::std::mem::size_of::<GhosttyWriter>() - 16usize];
    ["Alignment of GhosttyWriter"][::std::mem::align_of::<GhosttyWriter>() - 8usize];
    ["Offset of field: GhosttyWriter::write"]
        [::std::mem::offset_of!(GhosttyWriter, write) - 0usize];
    ["Offset of field: GhosttyWriter::userdata"]
        [::std::mem::offset_of!(GhosttyWriter, userdata) - 8usize];
};
impl Default for GhosttyWriter {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Read one MIME-typed representation of some content, streaming its\n bytes to a writer.\n\n The library calls this with the MIME type of the representation it\n needs. The callback writes all of that representation's data to\n @p writer, in as many calls to `writer.write(writer.userdata, data,\n len)` as is convenient (one call with everything or many small\n pieces both work), and returns true. Nothing written is retained\n beyond each write call, so the data may be borrowed from anywhere:\n a pasteboard item, a file being read, a stream.\n\n Returning false reports that the data could not be read. If the\n writer refuses a write (returns false), stop and return false\n without writing more.\n\n All pointer arguments, the mime, and the writer are borrowed and\n valid only for the duration of the callback. The callback is\n invoked synchronously on the calling thread. The API receiving the\n GhosttyMimeReader defines which MIME types are requested, how many\n times, and any consistency requirements across repeated reads.\n\n @param userdata Opaque userdata from GhosttyMimeReader\n @param mime The MIME type of the representation to read\n @param writer Where to write the data; valid only during this call\n @return true once all the data was written, false if it could not\n         be read or the writer refused a write"]
pub type GhosttyMimeReaderFn = ::std::option::Option<
    unsafe extern "C" fn(
        userdata: *mut ::std::os::raw::c_void,
        mime: GhosttyString,
        writer: GhosttyWriter,
    ) -> bool,
>;
#[doc = " A MIME-typed content source callback and its opaque context.\n\n The struct is passed by value. @p read must be non-NULL."]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyMimeReader {
    pub read: GhosttyMimeReaderFn,
    pub userdata: *mut ::std::os::raw::c_void,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyMimeReader"][::std::mem::size_of::<GhosttyMimeReader>() - 16usize];
    ["Alignment of GhosttyMimeReader"][::std::mem::align_of::<GhosttyMimeReader>() - 8usize];
    ["Offset of field: GhosttyMimeReader::read"]
        [::std::mem::offset_of!(GhosttyMimeReader, read) - 0usize];
    ["Offset of field: GhosttyMimeReader::userdata"]
        [::std::mem::offset_of!(GhosttyMimeReader, userdata) - 8usize];
};
impl Default for GhosttyMimeReader {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Packed cell value.\n\n Represents a single terminal cell. Portable callers can query fields via\n ghostty_cell_get(). Boundary-sensitive callers can decode the packed value\n using the GhosttyCell descriptor returned by ghostty_type_json(). The\n manifest is authoritative for the linked build; hardcoding bit positions\n is unsupported.\n\n @ingroup screen"]
pub type GhosttyCell = u64;
#[doc = " Opaque row value.\n\n Represents a single terminal row. The internal layout is opaque and\n must be queried via ghostty_row_get(). Obtain row values from\n terminal query APIs.\n\n @ingroup screen"]
pub type GhosttyRow = u64;
#[doc = " A borrowed view of contiguous raw cell values.\n\n The memory is not owned by this struct. The pointer is only valid\n for the lifetime documented by the API that produces it. Each value\n can be queried via ghostty_cell_get() or decoded using the GhosttyCell\n packed descriptor returned by ghostty_type_json().\n\n @ingroup screen"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyCellsView {
    #[doc = " Pointer to len contiguous cell values."]
    pub ptr: *const GhosttyCell,
    #[doc = " Number of cells."]
    pub len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyCellsView"][::std::mem::size_of::<GhosttyCellsView>() - 16usize];
    ["Alignment of GhosttyCellsView"][::std::mem::align_of::<GhosttyCellsView>() - 8usize];
    ["Offset of field: GhosttyCellsView::ptr"]
        [::std::mem::offset_of!(GhosttyCellsView, ptr) - 0usize];
    ["Offset of field: GhosttyCellsView::len"]
        [::std::mem::offset_of!(GhosttyCellsView, len) - 8usize];
};
impl Default for GhosttyCellsView {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " A single codepoint (may be zero for empty)."]
pub const GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_CODEPOINT: GhosttyCellContentTag = 0;
#[doc = " A codepoint that is part of a multi-codepoint grapheme cluster."]
pub const GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_CODEPOINT_GRAPHEME: GhosttyCellContentTag = 1;
#[doc = " No text; background color from palette."]
pub const GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_BG_COLOR_PALETTE: GhosttyCellContentTag = 2;
#[doc = " No text; background color as RGB."]
pub const GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_BG_COLOR_RGB: GhosttyCellContentTag = 3;
#[doc = " No text; background color as RGB."]
pub const GhosttyCellContentTag_GHOSTTY_CELL_CONTENT_TAG_MAX_VALUE: GhosttyCellContentTag =
    2147483647;
#[doc = " Cell content tag.\n\n Describes what kind of content a cell holds.\n\n @ingroup screen"]
pub type GhosttyCellContentTag = ::std::os::raw::c_int;
#[doc = " Not a wide character, cell width 1."]
pub const GhosttyCellWide_GHOSTTY_CELL_WIDE_NARROW: GhosttyCellWide = 0;
#[doc = " Wide character, cell width 2."]
pub const GhosttyCellWide_GHOSTTY_CELL_WIDE_WIDE: GhosttyCellWide = 1;
#[doc = " Spacer after wide character. Do not render."]
pub const GhosttyCellWide_GHOSTTY_CELL_WIDE_SPACER_TAIL: GhosttyCellWide = 2;
#[doc = " Spacer at end of soft-wrapped line for a wide character."]
pub const GhosttyCellWide_GHOSTTY_CELL_WIDE_SPACER_HEAD: GhosttyCellWide = 3;
#[doc = " Spacer at end of soft-wrapped line for a wide character."]
pub const GhosttyCellWide_GHOSTTY_CELL_WIDE_MAX_VALUE: GhosttyCellWide = 2147483647;
#[doc = " Cell wide property.\n\n Describes the width behavior of a cell.\n\n @ingroup screen"]
pub type GhosttyCellWide = ::std::os::raw::c_int;
#[doc = " Regular output content, such as command output."]
pub const GhosttyCellSemanticContent_GHOSTTY_CELL_SEMANTIC_OUTPUT: GhosttyCellSemanticContent = 0;
#[doc = " Content that is part of user input."]
pub const GhosttyCellSemanticContent_GHOSTTY_CELL_SEMANTIC_INPUT: GhosttyCellSemanticContent = 1;
#[doc = " Content that is part of a shell prompt."]
pub const GhosttyCellSemanticContent_GHOSTTY_CELL_SEMANTIC_PROMPT: GhosttyCellSemanticContent = 2;
#[doc = " Content that is part of a shell prompt."]
pub const GhosttyCellSemanticContent_GHOSTTY_CELL_SEMANTIC_MAX_VALUE: GhosttyCellSemanticContent =
    2147483647;
#[doc = " Semantic content type of a cell.\n\n Set by semantic prompt sequences (OSC 133) to distinguish between\n command output, user input, and shell prompt text.\n\n @ingroup screen"]
pub type GhosttyCellSemanticContent = ::std::os::raw::c_int;
#[doc = " Invalid data type. Never results in any data extraction."]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_INVALID: GhosttyCellData = 0;
#[doc = " The codepoint of the cell (0 if empty or bg-color-only).\n\n Output type: uint32_t *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_CODEPOINT: GhosttyCellData = 1;
#[doc = " The content tag describing what kind of content is in the cell.\n\n Output type: GhosttyCellContentTag *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_CONTENT_TAG: GhosttyCellData = 2;
#[doc = " The wide property of the cell.\n\n Output type: GhosttyCellWide *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_WIDE: GhosttyCellData = 3;
#[doc = " Whether the cell has text to render.\n\n Output type: bool *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_HAS_TEXT: GhosttyCellData = 4;
#[doc = " Whether the cell has non-default styling.\n\n Output type: bool *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_HAS_STYLING: GhosttyCellData = 5;
#[doc = " The style ID for the cell (for use with style lookups).\n\n Output type: uint16_t *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_STYLE_ID: GhosttyCellData = 6;
#[doc = " Whether the cell has a hyperlink.\n\n Output type: bool *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_HAS_HYPERLINK: GhosttyCellData = 7;
#[doc = " Whether the cell is protected.\n\n Output type: bool *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_PROTECTED: GhosttyCellData = 8;
#[doc = " The semantic content type of the cell (from OSC 133).\n\n Output type: GhosttyCellSemanticContent *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_SEMANTIC_CONTENT: GhosttyCellData = 9;
#[doc = " The palette index for the cell's background color.\n Only valid when content_tag is GHOSTTY_CELL_CONTENT_BG_COLOR_PALETTE.\n\n Output type: GhosttyColorPaletteIndex *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_COLOR_PALETTE: GhosttyCellData = 10;
#[doc = " The RGB value for the cell's background color.\n Only valid when content_tag is GHOSTTY_CELL_CONTENT_BG_COLOR_RGB.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_COLOR_RGB: GhosttyCellData = 11;
#[doc = " The RGB value for the cell's background color.\n Only valid when content_tag is GHOSTTY_CELL_CONTENT_BG_COLOR_RGB.\n\n Output type: GhosttyColorRgb *"]
pub const GhosttyCellData_GHOSTTY_CELL_DATA_MAX_VALUE: GhosttyCellData = 2147483647;
#[doc = " Cell data types.\n\n These values specify what type of data to extract from a cell\n using `ghostty_cell_get`.\n\n @ingroup screen"]
pub type GhosttyCellData = ::std::os::raw::c_int;
#[doc = " No prompt cells in this row."]
pub const GhosttyRowSemanticPrompt_GHOSTTY_ROW_SEMANTIC_NONE: GhosttyRowSemanticPrompt = 0;
#[doc = " Prompt cells exist and this is a primary prompt line."]
pub const GhosttyRowSemanticPrompt_GHOSTTY_ROW_SEMANTIC_PROMPT: GhosttyRowSemanticPrompt = 1;
#[doc = " Prompt cells exist and this is a continuation line."]
pub const GhosttyRowSemanticPrompt_GHOSTTY_ROW_SEMANTIC_PROMPT_CONTINUATION:
    GhosttyRowSemanticPrompt = 2;
#[doc = " Prompt cells exist and this is a continuation line."]
pub const GhosttyRowSemanticPrompt_GHOSTTY_ROW_SEMANTIC_MAX_VALUE: GhosttyRowSemanticPrompt =
    2147483647;
#[doc = " Row semantic prompt state.\n\n Indicates whether any cells in a row are part of a shell prompt,\n as reported by OSC 133 sequences.\n\n @ingroup screen"]
pub type GhosttyRowSemanticPrompt = ::std::os::raw::c_int;
#[doc = " Invalid data type. Never results in any data extraction."]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_INVALID: GhosttyRowData = 0;
#[doc = " Whether this row is soft-wrapped.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_WRAP: GhosttyRowData = 1;
#[doc = " Whether this row is a continuation of a soft-wrapped row.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_WRAP_CONTINUATION: GhosttyRowData = 2;
#[doc = " Whether any cells in this row have grapheme clusters.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_GRAPHEME: GhosttyRowData = 3;
#[doc = " Whether any cells in this row have styling (may have false positives).\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_STYLED: GhosttyRowData = 4;
#[doc = " Whether any cells in this row have hyperlinks (may have false positives).\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_HYPERLINK: GhosttyRowData = 5;
#[doc = " The semantic prompt state of this row.\n\n Output type: GhosttyRowSemanticPrompt *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_SEMANTIC_PROMPT: GhosttyRowData = 6;
#[doc = " Whether this row contains a Kitty virtual placeholder.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_KITTY_VIRTUAL_PLACEHOLDER: GhosttyRowData = 7;
#[doc = " Whether this row is dirty and requires a redraw.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_DIRTY: GhosttyRowData = 8;
#[doc = " Whether this row is dirty and requires a redraw.\n\n Output type: bool *"]
pub const GhosttyRowData_GHOSTTY_ROW_DATA_MAX_VALUE: GhosttyRowData = 2147483647;
#[doc = " Row data types.\n\n These values specify what type of data to extract from a row\n using `ghostty_row_get`.\n\n @ingroup screen"]
pub type GhosttyRowData = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Get data from a cell.\n\n Extracts typed data from the given cell based on the specified\n data type. The output pointer must be of the appropriate type for the\n requested data kind. Valid data types and output types are documented\n in the `GhosttyCellData` enum.\n\n @param cell The cell value\n @param data The type of data to extract\n @param out Pointer to store the extracted data (type depends on data parameter)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the\n         data type is invalid\n\n @ingroup screen"]
    pub fn ghostty_cell_get(
        cell: GhosttyCell,
        data: GhosttyCellData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from a cell in a single call.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param cell The cell value\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup screen"]
    pub fn ghostty_cell_get_multi(
        cell: GhosttyCell,
        count: usize,
        keys: *const GhosttyCellData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get data from a row.\n\n Extracts typed data from the given row based on the specified\n data type. The output pointer must be of the appropriate type for the\n requested data kind. Valid data types and output types are documented\n in the `GhosttyRowData` enum.\n\n @param row The row value\n @param data The type of data to extract\n @param out Pointer to store the extracted data (type depends on data parameter)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the\n         data type is invalid\n\n @ingroup screen"]
    pub fn ghostty_row_get(
        row: GhosttyRow,
        data: GhosttyRowData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from a row in a single call.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param row The row value\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup screen"]
    pub fn ghostty_row_get_multi(
        row: GhosttyRow,
        count: usize,
        keys: *const GhosttyRowData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " Style identifier type.\n\n Used to look up the full style from a grid reference.\n Obtain this from a cell via GHOSTTY_CELL_DATA_STYLE_ID.\n\n @ingroup style"]
pub type GhosttyStyleId = u16;
pub const GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_NONE: GhosttyStyleColorTag = 0;
pub const GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_PALETTE: GhosttyStyleColorTag = 1;
pub const GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_RGB: GhosttyStyleColorTag = 2;
pub const GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_TAG_MAX_VALUE: GhosttyStyleColorTag = 2147483647;
#[doc = " Style color tags.\n\n These values identify the type of color in a style color.\n Use the tag to determine which field in the color value union to access.\n\n @ingroup style"]
pub type GhosttyStyleColorTag = ::std::os::raw::c_int;
#[doc = " Style color value union.\n\n Use the tag to determine which field is active.\n\n @ingroup style"]
#[repr(C)]
#[derive(Copy, Clone)]
pub union GhosttyStyleColorValue {
    pub palette: GhosttyColorPaletteIndex,
    pub rgb: GhosttyColorRgb,
    pub _padding: u64,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyStyleColorValue"][::std::mem::size_of::<GhosttyStyleColorValue>() - 8usize];
    ["Alignment of GhosttyStyleColorValue"]
        [::std::mem::align_of::<GhosttyStyleColorValue>() - 8usize];
    ["Offset of field: GhosttyStyleColorValue::palette"]
        [::std::mem::offset_of!(GhosttyStyleColorValue, palette) - 0usize];
    ["Offset of field: GhosttyStyleColorValue::rgb"]
        [::std::mem::offset_of!(GhosttyStyleColorValue, rgb) - 0usize];
    ["Offset of field: GhosttyStyleColorValue::_padding"]
        [::std::mem::offset_of!(GhosttyStyleColorValue, _padding) - 0usize];
};
impl Default for GhosttyStyleColorValue {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Style color (tagged union).\n\n A color used in a style attribute. Can be unset (none), a palette\n index, or a direct RGB value.\n\n @ingroup style"]
#[repr(C)]
#[derive(Copy, Clone)]
pub struct GhosttyStyleColor {
    pub tag: GhosttyStyleColorTag,
    pub value: GhosttyStyleColorValue,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyStyleColor"][::std::mem::size_of::<GhosttyStyleColor>() - 16usize];
    ["Alignment of GhosttyStyleColor"][::std::mem::align_of::<GhosttyStyleColor>() - 8usize];
    ["Offset of field: GhosttyStyleColor::tag"]
        [::std::mem::offset_of!(GhosttyStyleColor, tag) - 0usize];
    ["Offset of field: GhosttyStyleColor::value"]
        [::std::mem::offset_of!(GhosttyStyleColor, value) - 8usize];
};
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
pub type GhosttyPointTag = ::std::os::raw::c_int;
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
#[doc = " A caller-provided buffer of selections.\n\n This follows the same conventions as GhosttyBuffer: ptr may be NULL with\n cap 0 to query the required capacity. APIs that fill this type set len to\n the number of entries written on GHOSTTY_SUCCESS, or to the required entry\n capacity on GHOSTTY_OUT_OF_SPACE.\n\n @ingroup selection"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySelectionBuffer {
    #[doc = " Destination buffer for selections. May be NULL when cap is 0 to query\n the required capacity."]
    pub ptr: *mut GhosttySelection,
    #[doc = " Capacity of ptr in entries."]
    pub cap: usize,
    #[doc = " Entries written on success, or required entry capacity on\n GHOSTTY_OUT_OF_SPACE."]
    pub len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySelectionBuffer"][::std::mem::size_of::<GhosttySelectionBuffer>() - 24usize];
    ["Alignment of GhosttySelectionBuffer"]
        [::std::mem::align_of::<GhosttySelectionBuffer>() - 8usize];
    ["Offset of field: GhosttySelectionBuffer::ptr"]
        [::std::mem::offset_of!(GhosttySelectionBuffer, ptr) - 0usize];
    ["Offset of field: GhosttySelectionBuffer::cap"]
        [::std::mem::offset_of!(GhosttySelectionBuffer, cap) - 8usize];
    ["Offset of field: GhosttySelectionBuffer::len"]
        [::std::mem::offset_of!(GhosttySelectionBuffer, len) - 16usize];
};
impl Default for GhosttySelectionBuffer {
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
