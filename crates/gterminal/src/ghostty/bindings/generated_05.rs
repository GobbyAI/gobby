unsafe extern "C" {
    #[doc = " Set SGR parameters for parsing.\n\n Sets the SGR parameter list to parse. Parameters are the numeric values\n from a CSI SGR sequence (e.g., for `ESC[1;31m`, params would be {1, 31}).\n\n The separators array optionally specifies the separator type for each\n parameter position. Each byte should be either ';' for semicolon or ':'\n for colon. This is needed for certain color formats that use colon\n separators (e.g., `ESC[4:3m` for curly underline). Any invalid separator\n values are treated as semicolons. The separators array must have the same\n length as the params array, if it is not NULL.\n\n If separators is NULL, all parameters are assumed to be semicolon-separated.\n\n This function makes an internal copy of the parameter and separator data,\n so the caller can safely free or modify the input arrays after this call.\n\n After calling this function, the parser is automatically reset and ready\n to iterate from the beginning.\n\n @param parser The parser handle, must not be NULL\n @param params Array of SGR parameter values\n @param separators Optional array of separator characters (';' or ':'), or\n NULL\n @param len Number of parameters (and separators if provided)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup sgr"]
    pub fn ghostty_sgr_set_params(
        parser: GhosttySgrParser,
        params: *const u16,
        separators: *const ::std::os::raw::c_char,
        len: usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get the next SGR attribute.\n\n Parses and returns the next attribute from the parameter list.\n Call this function repeatedly until it returns false to process\n all attributes in the sequence.\n\n @param parser The parser handle, must not be NULL\n @param attr Pointer to store the next attribute\n @return true if an attribute was returned, false if no more attributes\n\n @ingroup sgr"]
    pub fn ghostty_sgr_next(parser: GhosttySgrParser, attr: *mut GhosttySgrAttribute) -> bool;
}
unsafe extern "C" {
    #[doc = " Get the full parameter list from an unknown SGR attribute.\n\n This function retrieves the full parameter list that was provided to the\n parser when an unknown attribute was encountered. Primarily useful in\n WebAssembly environments where accessing struct fields directly is difficult.\n\n @param unknown The unknown attribute data\n @param ptr Pointer to store the pointer to the parameter array (may be NULL)\n @return The length of the full parameter array\n\n @ingroup sgr"]
    pub fn ghostty_sgr_unknown_full(unknown: GhosttySgrUnknown, ptr: *mut *const u16) -> usize;
}
unsafe extern "C" {
    #[doc = " Get the partial parameter list from an unknown SGR attribute.\n\n This function retrieves the partial parameter list where parsing stopped\n when an unknown attribute was encountered. Primarily useful in WebAssembly\n environments where accessing struct fields directly is difficult.\n\n @param unknown The unknown attribute data\n @param ptr Pointer to store the pointer to the parameter array (may be NULL)\n @return The length of the partial parameter array\n\n @ingroup sgr"]
    pub fn ghostty_sgr_unknown_partial(unknown: GhosttySgrUnknown, ptr: *mut *const u16) -> usize;
}
unsafe extern "C" {
    #[doc = " Get the tag from an SGR attribute.\n\n This function extracts the tag that identifies which type of attribute\n this is. Primarily useful in WebAssembly environments where accessing\n struct fields directly is difficult.\n\n @param attr The SGR attribute\n @return The attribute tag\n\n @ingroup sgr"]
    pub fn ghostty_sgr_attribute_tag(attr: GhosttySgrAttribute) -> GhosttySgrAttributeTag;
}
unsafe extern "C" {
    #[doc = " Get the value from an SGR attribute.\n\n This function returns a pointer to the value union from an SGR attribute. Use\n the tag to determine which field of the union is valid. Primarily useful in\n WebAssembly environments where accessing struct fields directly is difficult.\n\n @param attr Pointer to the SGR attribute\n @return Pointer to the attribute value union\n\n @ingroup sgr"]
    pub fn ghostty_sgr_attribute_value(
        attr: *mut GhosttySgrAttribute,
    ) -> *mut GhosttySgrAttributeValue;
}
#[doc = " Result of decoding an image.\n\n The `data` buffer must be allocated through the allocator provided to\n the decode callback. The library takes ownership and will free it\n with the same allocator."]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttySysImage {
    #[doc = " Image width in pixels."]
    pub width: u32,
    #[doc = " Image height in pixels."]
    pub height: u32,
    #[doc = " Pointer to the decoded RGBA pixel data."]
    pub data: *mut u8,
    #[doc = " Length of the pixel data in bytes."]
    pub data_len: usize,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttySysImage"][::std::mem::size_of::<GhosttySysImage>() - 24usize];
    ["Alignment of GhosttySysImage"][::std::mem::align_of::<GhosttySysImage>() - 8usize];
    ["Offset of field: GhosttySysImage::width"]
        [::std::mem::offset_of!(GhosttySysImage, width) - 0usize];
    ["Offset of field: GhosttySysImage::height"]
        [::std::mem::offset_of!(GhosttySysImage, height) - 4usize];
    ["Offset of field: GhosttySysImage::data"]
        [::std::mem::offset_of!(GhosttySysImage, data) - 8usize];
    ["Offset of field: GhosttySysImage::data_len"]
        [::std::mem::offset_of!(GhosttySysImage, data_len) - 16usize];
};
impl Default for GhosttySysImage {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
pub const GhosttySysLogLevel_GHOSTTY_SYS_LOG_LEVEL_ERROR: GhosttySysLogLevel = 0;
pub const GhosttySysLogLevel_GHOSTTY_SYS_LOG_LEVEL_WARNING: GhosttySysLogLevel = 1;
pub const GhosttySysLogLevel_GHOSTTY_SYS_LOG_LEVEL_INFO: GhosttySysLogLevel = 2;
pub const GhosttySysLogLevel_GHOSTTY_SYS_LOG_LEVEL_DEBUG: GhosttySysLogLevel = 3;
pub const GhosttySysLogLevel_GHOSTTY_SYS_LOG_LEVEL_MAX_VALUE: GhosttySysLogLevel = 2147483647;
#[doc = " Log severity levels for the log callback."]
pub type GhosttySysLogLevel = ::std::os::raw::c_uint;
#[doc = " Callback type for logging.\n\n When installed, internal library log messages are delivered through\n this callback instead of being discarded. The embedder is responsible\n for formatting and routing log output.\n\n @p scope is the log scope name as UTF-8 bytes (e.g. \"osc\", \"kitty\").\n When the log is unscoped (default scope), @p scope_len is 0.\n\n All pointer arguments are only valid for the duration of the callback.\n The callback must be safe to call from any thread.\n\n @param userdata    The userdata pointer set via GHOSTTY_SYS_OPT_USERDATA\n @param level       The severity level of the log message\n @param scope       Pointer to the scope name bytes\n @param scope_len   Length of the scope name in bytes\n @param message     Pointer to the log message bytes\n @param message_len Length of the log message in bytes"]
pub type GhosttySysLogFn = ::std::option::Option<
    unsafe extern "C" fn(
        userdata: *mut ::std::os::raw::c_void,
        level: GhosttySysLogLevel,
        scope: *const u8,
        scope_len: usize,
        message: *const u8,
        message_len: usize,
    ),
>;
#[doc = " Callback type for PNG decoding.\n\n Decodes raw PNG data into RGBA pixels. The output pixel data must be\n allocated through the provided allocator. The library takes ownership\n of the buffer and will free it with the same allocator.\n\n @param userdata  The userdata pointer set via GHOSTTY_SYS_OPT_USERDATA\n @param allocator The allocator to use for the output pixel buffer\n @param data      Pointer to the raw PNG data\n @param data_len  Length of the raw PNG data in bytes\n @param[out] out  On success, filled with the decoded image\n @return true on success, false on failure"]
pub type GhosttySysDecodePngFn = ::std::option::Option<
    unsafe extern "C" fn(
        userdata: *mut ::std::os::raw::c_void,
        allocator: *const GhosttyAllocator,
        data: *const u8,
        data_len: usize,
        out: *mut GhosttySysImage,
    ) -> bool,
>;
#[doc = " Set the userdata pointer passed to all sys callbacks.\n\n Input type: void* (or NULL)"]
pub const GhosttySysOption_GHOSTTY_SYS_OPT_USERDATA: GhosttySysOption = 0;
#[doc = " Set the PNG decode function.\n\n When set, the terminal can accept PNG images via the Kitty\n Graphics Protocol. When cleared (NULL value), PNG decoding is\n unsupported and PNG image data will be rejected.\n\n Input type: GhosttySysDecodePngFn (function pointer, or NULL)"]
pub const GhosttySysOption_GHOSTTY_SYS_OPT_DECODE_PNG: GhosttySysOption = 1;
#[doc = " Set the log callback.\n\n When set, internal library log messages are delivered to this\n callback. When cleared (NULL value), log messages are silently\n discarded.\n\n Use ghostty_sys_log_stderr as a convenience callback that\n writes formatted messages to stderr.\n\n Which log levels are emitted depends on the build mode of the\n library and is not configurable at runtime. Debug builds emit\n all levels (debug and above). Release builds emit info and\n above; debug-level messages are compiled out entirely and will\n never reach the callback.\n\n Input type: GhosttySysLogFn (function pointer, or NULL)"]
pub const GhosttySysOption_GHOSTTY_SYS_OPT_LOG: GhosttySysOption = 2;
#[doc = " Set the log callback.\n\n When set, internal library log messages are delivered to this\n callback. When cleared (NULL value), log messages are silently\n discarded.\n\n Use ghostty_sys_log_stderr as a convenience callback that\n writes formatted messages to stderr.\n\n Which log levels are emitted depends on the build mode of the\n library and is not configurable at runtime. Debug builds emit\n all levels (debug and above). Release builds emit info and\n above; debug-level messages are compiled out entirely and will\n never reach the callback.\n\n Input type: GhosttySysLogFn (function pointer, or NULL)"]
pub const GhosttySysOption_GHOSTTY_SYS_OPT_MAX_VALUE: GhosttySysOption = 2147483647;
#[doc = " System option identifiers for ghostty_sys_set()."]
pub type GhosttySysOption = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Set a system-level option.\n\n Configures a process-global implementation function. These should be\n set once at startup before using any terminal functionality that\n depends on them.\n\n @param option The option to set\n @param value  Pointer to the value (type depends on the option),\n               or NULL to clear it\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the\n         option is not recognized"]
    pub fn ghostty_sys_set(
        option: GhosttySysOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Built-in log callback that writes to stderr.\n\n Formats each message as \"[level](scope): message\\n\".\n Can be passed directly to ghostty_sys_set():\n\n @code\n ghostty_sys_set(GHOSTTY_SYS_OPT_LOG, &ghostty_sys_log_stderr);\n @endcode"]
    pub fn ghostty_sys_log_stderr(
        userdata: *mut ::std::os::raw::c_void,
        level: GhosttySysLogLevel,
        scope: *const u8,
        scope_len: usize,
        message: *const u8,
        message_len: usize,
    );
}
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyKeyEventImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to a key event.\n\n This handle represents a keyboard input event containing information about\n the physical key pressed, modifiers, and generated text.\n\n @ingroup key"]
pub type GhosttyKeyEvent = *mut GhosttyKeyEventImpl;
#[doc = " Key was released"]
pub const GhosttyKeyAction_GHOSTTY_KEY_ACTION_RELEASE: GhosttyKeyAction = 0;
#[doc = " Key was pressed"]
pub const GhosttyKeyAction_GHOSTTY_KEY_ACTION_PRESS: GhosttyKeyAction = 1;
#[doc = " Key is being repeated (held down)"]
pub const GhosttyKeyAction_GHOSTTY_KEY_ACTION_REPEAT: GhosttyKeyAction = 2;
#[doc = " Key is being repeated (held down)"]
pub const GhosttyKeyAction_GHOSTTY_KEY_ACTION_MAX_VALUE: GhosttyKeyAction = 2147483647;
#[doc = " Keyboard input event types.\n\n @ingroup key"]
pub type GhosttyKeyAction = ::std::os::raw::c_uint;
#[doc = " Keyboard modifier keys bitmask.\n\n A bitmask representing all keyboard modifiers. This tracks which modifier keys\n are pressed and, where supported by the platform, which side (left or right)\n of each modifier is active.\n\n Use the GHOSTTY_MODS_* constants to test and set individual modifiers.\n\n Modifier side bits are only meaningful when the corresponding modifier bit is set.\n Not all platforms support distinguishing between left and right modifier\n keys and Ghostty is built to expect that some platforms may not provide this\n information.\n\n @ingroup key"]
pub type GhosttyMods = u16;
pub const GhosttyKey_GHOSTTY_KEY_UNIDENTIFIED: GhosttyKey = 0;
pub const GhosttyKey_GHOSTTY_KEY_BACKQUOTE: GhosttyKey = 1;
pub const GhosttyKey_GHOSTTY_KEY_BACKSLASH: GhosttyKey = 2;
pub const GhosttyKey_GHOSTTY_KEY_BRACKET_LEFT: GhosttyKey = 3;
pub const GhosttyKey_GHOSTTY_KEY_BRACKET_RIGHT: GhosttyKey = 4;
pub const GhosttyKey_GHOSTTY_KEY_COMMA: GhosttyKey = 5;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_0: GhosttyKey = 6;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_1: GhosttyKey = 7;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_2: GhosttyKey = 8;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_3: GhosttyKey = 9;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_4: GhosttyKey = 10;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_5: GhosttyKey = 11;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_6: GhosttyKey = 12;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_7: GhosttyKey = 13;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_8: GhosttyKey = 14;
pub const GhosttyKey_GHOSTTY_KEY_DIGIT_9: GhosttyKey = 15;
pub const GhosttyKey_GHOSTTY_KEY_EQUAL: GhosttyKey = 16;
pub const GhosttyKey_GHOSTTY_KEY_INTL_BACKSLASH: GhosttyKey = 17;
pub const GhosttyKey_GHOSTTY_KEY_INTL_RO: GhosttyKey = 18;
pub const GhosttyKey_GHOSTTY_KEY_INTL_YEN: GhosttyKey = 19;
pub const GhosttyKey_GHOSTTY_KEY_A: GhosttyKey = 20;
pub const GhosttyKey_GHOSTTY_KEY_B: GhosttyKey = 21;
pub const GhosttyKey_GHOSTTY_KEY_C: GhosttyKey = 22;
pub const GhosttyKey_GHOSTTY_KEY_D: GhosttyKey = 23;
pub const GhosttyKey_GHOSTTY_KEY_E: GhosttyKey = 24;
pub const GhosttyKey_GHOSTTY_KEY_F: GhosttyKey = 25;
pub const GhosttyKey_GHOSTTY_KEY_G: GhosttyKey = 26;
pub const GhosttyKey_GHOSTTY_KEY_H: GhosttyKey = 27;
pub const GhosttyKey_GHOSTTY_KEY_I: GhosttyKey = 28;
pub const GhosttyKey_GHOSTTY_KEY_J: GhosttyKey = 29;
pub const GhosttyKey_GHOSTTY_KEY_K: GhosttyKey = 30;
pub const GhosttyKey_GHOSTTY_KEY_L: GhosttyKey = 31;
pub const GhosttyKey_GHOSTTY_KEY_M: GhosttyKey = 32;
pub const GhosttyKey_GHOSTTY_KEY_N: GhosttyKey = 33;
pub const GhosttyKey_GHOSTTY_KEY_O: GhosttyKey = 34;
pub const GhosttyKey_GHOSTTY_KEY_P: GhosttyKey = 35;
pub const GhosttyKey_GHOSTTY_KEY_Q: GhosttyKey = 36;
pub const GhosttyKey_GHOSTTY_KEY_R: GhosttyKey = 37;
pub const GhosttyKey_GHOSTTY_KEY_S: GhosttyKey = 38;
pub const GhosttyKey_GHOSTTY_KEY_T: GhosttyKey = 39;
pub const GhosttyKey_GHOSTTY_KEY_U: GhosttyKey = 40;
pub const GhosttyKey_GHOSTTY_KEY_V: GhosttyKey = 41;
pub const GhosttyKey_GHOSTTY_KEY_W: GhosttyKey = 42;
pub const GhosttyKey_GHOSTTY_KEY_X: GhosttyKey = 43;
pub const GhosttyKey_GHOSTTY_KEY_Y: GhosttyKey = 44;
pub const GhosttyKey_GHOSTTY_KEY_Z: GhosttyKey = 45;
pub const GhosttyKey_GHOSTTY_KEY_MINUS: GhosttyKey = 46;
pub const GhosttyKey_GHOSTTY_KEY_PERIOD: GhosttyKey = 47;
pub const GhosttyKey_GHOSTTY_KEY_QUOTE: GhosttyKey = 48;
pub const GhosttyKey_GHOSTTY_KEY_SEMICOLON: GhosttyKey = 49;
pub const GhosttyKey_GHOSTTY_KEY_SLASH: GhosttyKey = 50;
pub const GhosttyKey_GHOSTTY_KEY_ALT_LEFT: GhosttyKey = 51;
pub const GhosttyKey_GHOSTTY_KEY_ALT_RIGHT: GhosttyKey = 52;
pub const GhosttyKey_GHOSTTY_KEY_BACKSPACE: GhosttyKey = 53;
pub const GhosttyKey_GHOSTTY_KEY_CAPS_LOCK: GhosttyKey = 54;
pub const GhosttyKey_GHOSTTY_KEY_CONTEXT_MENU: GhosttyKey = 55;
pub const GhosttyKey_GHOSTTY_KEY_CONTROL_LEFT: GhosttyKey = 56;
pub const GhosttyKey_GHOSTTY_KEY_CONTROL_RIGHT: GhosttyKey = 57;
pub const GhosttyKey_GHOSTTY_KEY_ENTER: GhosttyKey = 58;
pub const GhosttyKey_GHOSTTY_KEY_META_LEFT: GhosttyKey = 59;
pub const GhosttyKey_GHOSTTY_KEY_META_RIGHT: GhosttyKey = 60;
pub const GhosttyKey_GHOSTTY_KEY_SHIFT_LEFT: GhosttyKey = 61;
pub const GhosttyKey_GHOSTTY_KEY_SHIFT_RIGHT: GhosttyKey = 62;
pub const GhosttyKey_GHOSTTY_KEY_SPACE: GhosttyKey = 63;
pub const GhosttyKey_GHOSTTY_KEY_TAB: GhosttyKey = 64;
pub const GhosttyKey_GHOSTTY_KEY_CONVERT: GhosttyKey = 65;
pub const GhosttyKey_GHOSTTY_KEY_KANA_MODE: GhosttyKey = 66;
pub const GhosttyKey_GHOSTTY_KEY_NON_CONVERT: GhosttyKey = 67;
pub const GhosttyKey_GHOSTTY_KEY_DELETE: GhosttyKey = 68;
pub const GhosttyKey_GHOSTTY_KEY_END: GhosttyKey = 69;
pub const GhosttyKey_GHOSTTY_KEY_HELP: GhosttyKey = 70;
pub const GhosttyKey_GHOSTTY_KEY_HOME: GhosttyKey = 71;
pub const GhosttyKey_GHOSTTY_KEY_INSERT: GhosttyKey = 72;
pub const GhosttyKey_GHOSTTY_KEY_PAGE_DOWN: GhosttyKey = 73;
pub const GhosttyKey_GHOSTTY_KEY_PAGE_UP: GhosttyKey = 74;
pub const GhosttyKey_GHOSTTY_KEY_ARROW_DOWN: GhosttyKey = 75;
pub const GhosttyKey_GHOSTTY_KEY_ARROW_LEFT: GhosttyKey = 76;
pub const GhosttyKey_GHOSTTY_KEY_ARROW_RIGHT: GhosttyKey = 77;
pub const GhosttyKey_GHOSTTY_KEY_ARROW_UP: GhosttyKey = 78;
pub const GhosttyKey_GHOSTTY_KEY_NUM_LOCK: GhosttyKey = 79;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_0: GhosttyKey = 80;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_1: GhosttyKey = 81;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_2: GhosttyKey = 82;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_3: GhosttyKey = 83;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_4: GhosttyKey = 84;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_5: GhosttyKey = 85;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_6: GhosttyKey = 86;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_7: GhosttyKey = 87;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_8: GhosttyKey = 88;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_9: GhosttyKey = 89;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_ADD: GhosttyKey = 90;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_BACKSPACE: GhosttyKey = 91;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_CLEAR: GhosttyKey = 92;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_CLEAR_ENTRY: GhosttyKey = 93;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_COMMA: GhosttyKey = 94;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_DECIMAL: GhosttyKey = 95;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_DIVIDE: GhosttyKey = 96;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_ENTER: GhosttyKey = 97;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_EQUAL: GhosttyKey = 98;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MEMORY_ADD: GhosttyKey = 99;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MEMORY_CLEAR: GhosttyKey = 100;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MEMORY_RECALL: GhosttyKey = 101;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MEMORY_STORE: GhosttyKey = 102;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MEMORY_SUBTRACT: GhosttyKey = 103;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_MULTIPLY: GhosttyKey = 104;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_PAREN_LEFT: GhosttyKey = 105;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_PAREN_RIGHT: GhosttyKey = 106;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_SUBTRACT: GhosttyKey = 107;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_SEPARATOR: GhosttyKey = 108;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_UP: GhosttyKey = 109;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_DOWN: GhosttyKey = 110;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_RIGHT: GhosttyKey = 111;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_LEFT: GhosttyKey = 112;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_BEGIN: GhosttyKey = 113;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_HOME: GhosttyKey = 114;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_END: GhosttyKey = 115;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_INSERT: GhosttyKey = 116;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_DELETE: GhosttyKey = 117;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_PAGE_UP: GhosttyKey = 118;
pub const GhosttyKey_GHOSTTY_KEY_NUMPAD_PAGE_DOWN: GhosttyKey = 119;
pub const GhosttyKey_GHOSTTY_KEY_ESCAPE: GhosttyKey = 120;
pub const GhosttyKey_GHOSTTY_KEY_F1: GhosttyKey = 121;
pub const GhosttyKey_GHOSTTY_KEY_F2: GhosttyKey = 122;
pub const GhosttyKey_GHOSTTY_KEY_F3: GhosttyKey = 123;
pub const GhosttyKey_GHOSTTY_KEY_F4: GhosttyKey = 124;
pub const GhosttyKey_GHOSTTY_KEY_F5: GhosttyKey = 125;
pub const GhosttyKey_GHOSTTY_KEY_F6: GhosttyKey = 126;
pub const GhosttyKey_GHOSTTY_KEY_F7: GhosttyKey = 127;
pub const GhosttyKey_GHOSTTY_KEY_F8: GhosttyKey = 128;
pub const GhosttyKey_GHOSTTY_KEY_F9: GhosttyKey = 129;
pub const GhosttyKey_GHOSTTY_KEY_F10: GhosttyKey = 130;
pub const GhosttyKey_GHOSTTY_KEY_F11: GhosttyKey = 131;
pub const GhosttyKey_GHOSTTY_KEY_F12: GhosttyKey = 132;
pub const GhosttyKey_GHOSTTY_KEY_F13: GhosttyKey = 133;
pub const GhosttyKey_GHOSTTY_KEY_F14: GhosttyKey = 134;
pub const GhosttyKey_GHOSTTY_KEY_F15: GhosttyKey = 135;
pub const GhosttyKey_GHOSTTY_KEY_F16: GhosttyKey = 136;
pub const GhosttyKey_GHOSTTY_KEY_F17: GhosttyKey = 137;
pub const GhosttyKey_GHOSTTY_KEY_F18: GhosttyKey = 138;
pub const GhosttyKey_GHOSTTY_KEY_F19: GhosttyKey = 139;
pub const GhosttyKey_GHOSTTY_KEY_F20: GhosttyKey = 140;
pub const GhosttyKey_GHOSTTY_KEY_F21: GhosttyKey = 141;
pub const GhosttyKey_GHOSTTY_KEY_F22: GhosttyKey = 142;
pub const GhosttyKey_GHOSTTY_KEY_F23: GhosttyKey = 143;
pub const GhosttyKey_GHOSTTY_KEY_F24: GhosttyKey = 144;
pub const GhosttyKey_GHOSTTY_KEY_F25: GhosttyKey = 145;
pub const GhosttyKey_GHOSTTY_KEY_FN: GhosttyKey = 146;
pub const GhosttyKey_GHOSTTY_KEY_FN_LOCK: GhosttyKey = 147;
pub const GhosttyKey_GHOSTTY_KEY_PRINT_SCREEN: GhosttyKey = 148;
pub const GhosttyKey_GHOSTTY_KEY_SCROLL_LOCK: GhosttyKey = 149;
pub const GhosttyKey_GHOSTTY_KEY_PAUSE: GhosttyKey = 150;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_BACK: GhosttyKey = 151;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_FAVORITES: GhosttyKey = 152;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_FORWARD: GhosttyKey = 153;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_HOME: GhosttyKey = 154;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_REFRESH: GhosttyKey = 155;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_SEARCH: GhosttyKey = 156;
pub const GhosttyKey_GHOSTTY_KEY_BROWSER_STOP: GhosttyKey = 157;
pub const GhosttyKey_GHOSTTY_KEY_EJECT: GhosttyKey = 158;
pub const GhosttyKey_GHOSTTY_KEY_LAUNCH_APP_1: GhosttyKey = 159;
pub const GhosttyKey_GHOSTTY_KEY_LAUNCH_APP_2: GhosttyKey = 160;
pub const GhosttyKey_GHOSTTY_KEY_LAUNCH_MAIL: GhosttyKey = 161;
pub const GhosttyKey_GHOSTTY_KEY_MEDIA_PLAY_PAUSE: GhosttyKey = 162;
pub const GhosttyKey_GHOSTTY_KEY_MEDIA_SELECT: GhosttyKey = 163;
pub const GhosttyKey_GHOSTTY_KEY_MEDIA_STOP: GhosttyKey = 164;
pub const GhosttyKey_GHOSTTY_KEY_MEDIA_TRACK_NEXT: GhosttyKey = 165;
pub const GhosttyKey_GHOSTTY_KEY_MEDIA_TRACK_PREVIOUS: GhosttyKey = 166;
pub const GhosttyKey_GHOSTTY_KEY_POWER: GhosttyKey = 167;
pub const GhosttyKey_GHOSTTY_KEY_SLEEP: GhosttyKey = 168;
pub const GhosttyKey_GHOSTTY_KEY_AUDIO_VOLUME_DOWN: GhosttyKey = 169;
pub const GhosttyKey_GHOSTTY_KEY_AUDIO_VOLUME_MUTE: GhosttyKey = 170;
pub const GhosttyKey_GHOSTTY_KEY_AUDIO_VOLUME_UP: GhosttyKey = 171;
pub const GhosttyKey_GHOSTTY_KEY_WAKE_UP: GhosttyKey = 172;
pub const GhosttyKey_GHOSTTY_KEY_COPY: GhosttyKey = 173;
pub const GhosttyKey_GHOSTTY_KEY_CUT: GhosttyKey = 174;
pub const GhosttyKey_GHOSTTY_KEY_PASTE: GhosttyKey = 175;
pub const GhosttyKey_GHOSTTY_KEY_MAX_VALUE: GhosttyKey = 2147483647;
#[doc = " Physical key codes.\n\n The set of key codes that Ghostty is aware of. These represent physical keys\n on the keyboard and are layout-independent. For example, the \"a\" key on a US\n keyboard is the same as the \"ф\" key on a Russian keyboard, but both will\n report the same key_a value.\n\n Layout-dependent strings are provided separately as UTF-8 text and are produced\n by the platform. These values are based on the W3C UI Events KeyboardEvent code\n standard. See: https://www.w3.org/TR/uievents-code\n\n @ingroup key"]
pub type GhosttyKey = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Create a new key event instance.\n\n Creates a new key event with default values. The event must be freed using\n ghostty_key_event_free() when no longer needed.\n\n @param allocator Pointer to the allocator to use for memory management, or NULL to use the default allocator\n @param event Pointer to store the created key event handle\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup key"]
    pub fn ghostty_key_event_new(
        allocator: *const GhosttyAllocator,
        event: *mut GhosttyKeyEvent,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a key event instance.\n\n Releases all resources associated with the key event. After this call,\n the event handle becomes invalid and must not be used.\n\n @param event The key event handle to free (may be NULL)\n\n @ingroup key"]
    pub fn ghostty_key_event_free(event: GhosttyKeyEvent);
}
unsafe extern "C" {
    #[doc = " Set the key action (press, release, repeat).\n\n @param event The key event handle, must not be NULL\n @param action The action to set\n\n @ingroup key"]
    pub fn ghostty_key_event_set_action(event: GhosttyKeyEvent, action: GhosttyKeyAction);
}
unsafe extern "C" {
    #[doc = " Get the key action (press, release, repeat).\n\n @param event The key event handle, must not be NULL\n @return The key action\n\n @ingroup key"]
    pub fn ghostty_key_event_get_action(event: GhosttyKeyEvent) -> GhosttyKeyAction;
}
unsafe extern "C" {
    #[doc = " Set the physical key code.\n\n @param event The key event handle, must not be NULL\n @param key The physical key code to set\n\n @ingroup key"]
    pub fn ghostty_key_event_set_key(event: GhosttyKeyEvent, key: GhosttyKey);
}
unsafe extern "C" {
    #[doc = " Get the physical key code.\n\n @param event The key event handle, must not be NULL\n @return The physical key code\n\n @ingroup key"]
    pub fn ghostty_key_event_get_key(event: GhosttyKeyEvent) -> GhosttyKey;
}
unsafe extern "C" {
    #[doc = " Set the modifier keys bitmask.\n\n @param event The key event handle, must not be NULL\n @param mods The modifier keys bitmask to set\n\n @ingroup key"]
    pub fn ghostty_key_event_set_mods(event: GhosttyKeyEvent, mods: GhosttyMods);
}
unsafe extern "C" {
    #[doc = " Get the modifier keys bitmask.\n\n @param event The key event handle, must not be NULL\n @return The modifier keys bitmask\n\n @ingroup key"]
    pub fn ghostty_key_event_get_mods(event: GhosttyKeyEvent) -> GhosttyMods;
}
unsafe extern "C" {
    #[doc = " Set the consumed modifiers bitmask.\n\n @param event The key event handle, must not be NULL\n @param consumed_mods The consumed modifiers bitmask to set\n\n @ingroup key"]
    pub fn ghostty_key_event_set_consumed_mods(event: GhosttyKeyEvent, consumed_mods: GhosttyMods);
}
unsafe extern "C" {
    #[doc = " Get the consumed modifiers bitmask.\n\n @param event The key event handle, must not be NULL\n @return The consumed modifiers bitmask\n\n @ingroup key"]
    pub fn ghostty_key_event_get_consumed_mods(event: GhosttyKeyEvent) -> GhosttyMods;
}
unsafe extern "C" {
    #[doc = " Set whether the key event is part of a composition sequence.\n\n @param event The key event handle, must not be NULL\n @param composing Whether the key event is part of a composition sequence\n\n @ingroup key"]
    pub fn ghostty_key_event_set_composing(event: GhosttyKeyEvent, composing: bool);
}
unsafe extern "C" {
    #[doc = " Get whether the key event is part of a composition sequence.\n\n @param event The key event handle, must not be NULL\n @return Whether the key event is part of a composition sequence\n\n @ingroup key"]
    pub fn ghostty_key_event_get_composing(event: GhosttyKeyEvent) -> bool;
}
unsafe extern "C" {
    #[doc = " Set the UTF-8 text generated by the key for the current keyboard layout.\n\n Must contain the unmodified character before any Ctrl/Meta transformations.\n The encoder derives modifier sequences from the logical key and mods\n bitmask, not from this text. Do not pass C0 control characters\n (U+0000-U+001F, U+007F) or platform function key codes (e.g. macOS PUA\n U+F700-U+F8FF); pass NULL instead and let the encoder use the logical key.\n\n The key event does NOT take ownership of the text pointer. The caller\n must ensure the string remains valid for the lifetime needed by the event.\n\n @param event The key event handle, must not be NULL\n @param utf8 The UTF-8 text to set (or NULL for empty)\n @param len Length of the UTF-8 text in bytes\n\n @ingroup key"]
    pub fn ghostty_key_event_set_utf8(
        event: GhosttyKeyEvent,
        utf8: *const ::std::os::raw::c_char,
        len: usize,
    );
}
unsafe extern "C" {
    #[doc = " Get the UTF-8 text generated by the key event.\n\n The returned pointer is valid until the event is freed or the UTF-8 text is modified.\n\n @param event The key event handle, must not be NULL\n @param len Pointer to store the length of the UTF-8 text in bytes (may be NULL)\n @return The UTF-8 text (or NULL for empty)\n\n @ingroup key"]
    pub fn ghostty_key_event_get_utf8(
        event: GhosttyKeyEvent,
        len: *mut usize,
    ) -> *const ::std::os::raw::c_char;
}
unsafe extern "C" {
    #[doc = " Set the unshifted Unicode codepoint.\n\n @param event The key event handle, must not be NULL\n @param codepoint The unshifted Unicode codepoint to set\n\n @ingroup key"]
    pub fn ghostty_key_event_set_unshifted_codepoint(event: GhosttyKeyEvent, codepoint: u32);
}
unsafe extern "C" {
    #[doc = " Get the unshifted Unicode codepoint.\n\n @param event The key event handle, must not be NULL\n @return The unshifted Unicode codepoint\n\n @ingroup key"]
    pub fn ghostty_key_event_get_unshifted_codepoint(event: GhosttyKeyEvent) -> u32;
}
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyKeyEncoderImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to a key encoder instance.\n\n This handle represents a key encoder that converts key events into terminal\n escape sequences.\n\n @ingroup key"]
pub type GhosttyKeyEncoder = *mut GhosttyKeyEncoderImpl;
#[doc = " Kitty keyboard protocol flags.\n\n Bitflags representing the various modes of the Kitty keyboard protocol.\n These can be combined using bitwise OR operations. Valid values all\n start with `GHOSTTY_KITTY_KEY_`.\n\n @ingroup key"]
pub type GhosttyKittyKeyFlags = u8;
#[doc = " Option key is not treated as alt"]
pub const GhosttyOptionAsAlt_GHOSTTY_OPTION_AS_ALT_FALSE: GhosttyOptionAsAlt = 0;
#[doc = " Option key is treated as alt"]
pub const GhosttyOptionAsAlt_GHOSTTY_OPTION_AS_ALT_TRUE: GhosttyOptionAsAlt = 1;
#[doc = " Only left option key is treated as alt"]
pub const GhosttyOptionAsAlt_GHOSTTY_OPTION_AS_ALT_LEFT: GhosttyOptionAsAlt = 2;
#[doc = " Only right option key is treated as alt"]
pub const GhosttyOptionAsAlt_GHOSTTY_OPTION_AS_ALT_RIGHT: GhosttyOptionAsAlt = 3;
#[doc = " Only right option key is treated as alt"]
pub const GhosttyOptionAsAlt_GHOSTTY_OPTION_AS_ALT_MAX_VALUE: GhosttyOptionAsAlt = 2147483647;
#[doc = " macOS option key behavior.\n\n Determines whether the \"option\" key on macOS is treated as \"alt\" or not.\n See the Ghostty `macos-option-as-alt` configuration option for more details.\n\n @ingroup key"]
pub type GhosttyOptionAsAlt = ::std::os::raw::c_uint;
#[doc = " Terminal DEC mode 1: cursor key application mode (value: bool)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_CURSOR_KEY_APPLICATION:
    GhosttyKeyEncoderOption = 0;
#[doc = " Terminal DEC mode 66: keypad key application mode (value: bool)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_KEYPAD_KEY_APPLICATION:
    GhosttyKeyEncoderOption = 1;
#[doc = " Terminal DEC mode 1035: ignore keypad with numlock (value: bool)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_IGNORE_KEYPAD_WITH_NUMLOCK:
    GhosttyKeyEncoderOption = 2;
#[doc = " Terminal DEC mode 1036: alt sends escape prefix (value: bool)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_ALT_ESC_PREFIX: GhosttyKeyEncoderOption =
    3;
#[doc = " xterm modifyOtherKeys mode 2 (value: bool)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_MODIFY_OTHER_KEYS_STATE_2:
    GhosttyKeyEncoderOption = 4;
#[doc = " Kitty keyboard protocol flags (value: GhosttyKittyKeyFlags bitmask)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_KITTY_FLAGS: GhosttyKeyEncoderOption = 5;
#[doc = " macOS option-as-alt setting (value: GhosttyOptionAsAlt)"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_MACOS_OPTION_AS_ALT:
    GhosttyKeyEncoderOption = 6;
#[doc = " Backarrow key mode (value: bool)\n See https://vt100.net/dec/ek-vt3xx-tp-002.pdf page 170\n If `false` (the default), `backspace` emits 0x7f\n If `true`, `backspace` emits 0x08"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_BACKARROW_KEY_MODE:
    GhosttyKeyEncoderOption = 7;
#[doc = " Backarrow key mode (value: bool)\n See https://vt100.net/dec/ek-vt3xx-tp-002.pdf page 170\n If `false` (the default), `backspace` emits 0x7f\n If `true`, `backspace` emits 0x08"]
pub const GhosttyKeyEncoderOption_GHOSTTY_KEY_ENCODER_OPT_MAX_VALUE: GhosttyKeyEncoderOption =
    2147483647;
#[doc = " Key encoder option identifiers.\n\n These values are used with ghostty_key_encoder_setopt() to configure\n the behavior of the key encoder.\n\n @ingroup key"]
pub type GhosttyKeyEncoderOption = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Create a new key encoder instance.\n\n Creates a new key encoder with default options. The encoder can be configured\n using ghostty_key_encoder_setopt() and must be freed using\n ghostty_key_encoder_free() when no longer needed.\n\n @param allocator Pointer to the allocator to use for memory management, or NULL to use the default allocator\n @param encoder Pointer to store the created encoder handle\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup key"]
    pub fn ghostty_key_encoder_new(
        allocator: *const GhosttyAllocator,
        encoder: *mut GhosttyKeyEncoder,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a key encoder instance.\n\n Releases all resources associated with the key encoder. After this call,\n the encoder handle becomes invalid and must not be used.\n\n @param encoder The encoder handle to free (may be NULL)\n\n @ingroup key"]
    pub fn ghostty_key_encoder_free(encoder: GhosttyKeyEncoder);
}
unsafe extern "C" {
    #[doc = " Set an option on the key encoder.\n\n Configures the behavior of the key encoder. Options control various aspects\n of encoding such as terminal modes (cursor key application mode, keypad mode),\n protocol selection (Kitty keyboard protocol flags), and platform-specific\n behaviors (macOS option-as-alt).\n\n If you are using a terminal instance, you can set the key encoding\n options based on the active terminal state (e.g. legacy vs Kitty mode\n and associated flags) with ghostty_key_encoder_setopt_from_terminal().\n\n A null pointer value does nothing. It does not reset the value to the\n default. The setopt call will do nothing.\n\n @param encoder The encoder handle, must not be NULL\n @param option The option to set\n @param value Pointer to the value to set (type depends on the option)\n\n @ingroup key"]
    pub fn ghostty_key_encoder_setopt(
        encoder: GhosttyKeyEncoder,
        option: GhosttyKeyEncoderOption,
        value: *const ::std::os::raw::c_void,
    );
}
unsafe extern "C" {
    #[doc = " Set encoder options from a terminal's current state.\n\n Reads the terminal's current modes and flags and applies them to the\n encoder's options. This sets cursor key application mode, keypad mode,\n alt escape prefix, modifyOtherKeys state, and Kitty keyboard protocol\n flags from the terminal state.\n\n Note that the `macos_option_as_alt` option cannot be determined from\n terminal state and is reset to `GHOSTTY_OPTION_AS_ALT_FALSE` by this\n call. Use ghostty_key_encoder_setopt() to set it afterward if needed.\n\n @param encoder The encoder handle, must not be NULL\n @param terminal The terminal handle, must not be NULL\n\n @ingroup key"]
    pub fn ghostty_key_encoder_setopt_from_terminal(
        encoder: GhosttyKeyEncoder,
        terminal: GhosttyTerminal,
    );
}
unsafe extern "C" {
    #[doc = " Encode a key event into a terminal escape sequence.\n\n Converts a key event into the appropriate terminal escape sequence based on\n the encoder's current options. The sequence is written to the provided buffer.\n\n Not all key events produce output. For example, unmodified modifier keys\n typically don't generate escape sequences. Check the out_len parameter to\n determine if any data was written.\n\n If the output buffer is too small, this function returns GHOSTTY_OUT_OF_SPACE\n and out_len will contain the required buffer size. The caller can then\n allocate a larger buffer and call the function again.\n\n @param encoder The encoder handle, must not be NULL\n @param event The key event to encode, must not be NULL\n @param out_buf Buffer to write the encoded sequence to\n @param out_buf_size Size of the output buffer in bytes\n @param out_len Pointer to store the number of bytes written (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if buffer too small, or other error code\n\n ## Example: Calculate required buffer size\n\n @code{.c}\n // Query the required size with a NULL buffer (always returns OUT_OF_SPACE)\n size_t required = 0;\n GhosttyResult result = ghostty_key_encoder_encode(encoder, event, NULL, 0, &required);\n assert(result == GHOSTTY_OUT_OF_SPACE);\n\n // Allocate buffer of required size\n char *buf = malloc(required);\n\n // Encode with properly sized buffer\n size_t written = 0;\n result = ghostty_key_encoder_encode(encoder, event, buf, required, &written);\n assert(result == GHOSTTY_SUCCESS);\n\n // Use the encoded sequence...\n\n free(buf);\n @endcode\n\n ## Example: Direct encoding with static buffer\n\n @code{.c}\n // Most escape sequences are short, so a static buffer often suffices\n char buf[128];\n size_t written = 0;\n GhosttyResult result = ghostty_key_encoder_encode(encoder, event, buf, sizeof(buf), &written);\n\n if (result == GHOSTTY_SUCCESS) {\n   // Write the encoded sequence to the terminal\n   write(pty_fd, buf, written);\n } else if (result == GHOSTTY_OUT_OF_SPACE) {\n   // Buffer too small, written contains required size\n   char *dynamic_buf = malloc(written);\n   result = ghostty_key_encoder_encode(encoder, event, dynamic_buf, written, &written);\n   assert(result == GHOSTTY_SUCCESS);\n   write(pty_fd, dynamic_buf, written);\n   free(dynamic_buf);\n }\n @endcode\n\n @ingroup key"]
    pub fn ghostty_key_encoder_encode(
        encoder: GhosttyKeyEncoder,
        event: GhosttyKeyEvent,
        out_buf: *mut ::std::os::raw::c_char,
        out_buf_size: usize,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyMouseEventImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to a mouse event.\n\n This handle represents a normalized mouse input event containing\n action, button, modifiers, and surface-space position.\n\n @ingroup mouse"]
pub type GhosttyMouseEvent = *mut GhosttyMouseEventImpl;
#[doc = " Mouse button was pressed."]
pub const GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_PRESS: GhosttyMouseAction = 0;
#[doc = " Mouse button was released."]
pub const GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_RELEASE: GhosttyMouseAction = 1;
#[doc = " Mouse moved."]
pub const GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_MOTION: GhosttyMouseAction = 2;
#[doc = " Mouse moved."]
pub const GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_MAX_VALUE: GhosttyMouseAction = 2147483647;
#[doc = " Mouse event action type.\n\n @ingroup mouse"]
pub type GhosttyMouseAction = ::std::os::raw::c_uint;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_UNKNOWN: GhosttyMouseButton = 0;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_LEFT: GhosttyMouseButton = 1;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_RIGHT: GhosttyMouseButton = 2;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_MIDDLE: GhosttyMouseButton = 3;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_FOUR: GhosttyMouseButton = 4;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_FIVE: GhosttyMouseButton = 5;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_SIX: GhosttyMouseButton = 6;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_SEVEN: GhosttyMouseButton = 7;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_EIGHT: GhosttyMouseButton = 8;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_NINE: GhosttyMouseButton = 9;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_TEN: GhosttyMouseButton = 10;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_ELEVEN: GhosttyMouseButton = 11;
pub const GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_MAX_VALUE: GhosttyMouseButton = 2147483647;
#[doc = " Mouse button identity.\n\n @ingroup mouse"]
pub type GhosttyMouseButton = ::std::os::raw::c_uint;
#[doc = " Mouse position in surface-space pixels.\n\n @ingroup mouse"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyMousePosition {
    pub x: f32,
    pub y: f32,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyMousePosition"][::std::mem::size_of::<GhosttyMousePosition>() - 8usize];
    ["Alignment of GhosttyMousePosition"][::std::mem::align_of::<GhosttyMousePosition>() - 4usize];
    ["Offset of field: GhosttyMousePosition::x"]
        [::std::mem::offset_of!(GhosttyMousePosition, x) - 0usize];
    ["Offset of field: GhosttyMousePosition::y"]
        [::std::mem::offset_of!(GhosttyMousePosition, y) - 4usize];
};
unsafe extern "C" {
    #[doc = " Create a new mouse event instance.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param event Pointer to store the created event handle\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_new(
        allocator: *const GhosttyAllocator,
        event: *mut GhosttyMouseEvent,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a mouse event instance.\n\n @param event The mouse event handle to free (may be NULL)\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_free(event: GhosttyMouseEvent);
}
unsafe extern "C" {
    #[doc = " Set the event action.\n\n @param event The event handle, must not be NULL\n @param action The action to set\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_set_action(event: GhosttyMouseEvent, action: GhosttyMouseAction);
}
unsafe extern "C" {
    #[doc = " Get the event action.\n\n @param event The event handle, must not be NULL\n @return The event action\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_get_action(event: GhosttyMouseEvent) -> GhosttyMouseAction;
}
unsafe extern "C" {
    #[doc = " Set the event button.\n\n This sets a concrete button identity for the event.\n To represent \"no button\" (for motion events), use\n ghostty_mouse_event_clear_button().\n\n @param event The event handle, must not be NULL\n @param button The button to set\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_set_button(event: GhosttyMouseEvent, button: GhosttyMouseButton);
}
unsafe extern "C" {
    #[doc = " Clear the event button.\n\n This sets the event button to \"none\".\n\n @param event The event handle, must not be NULL\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_clear_button(event: GhosttyMouseEvent);
}
unsafe extern "C" {
    #[doc = " Get the event button.\n\n @param event The event handle, must not be NULL\n @param out_button Output pointer for the button value (may be NULL)\n @return true if a button is set, false if no button is set\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_get_button(
        event: GhosttyMouseEvent,
        out_button: *mut GhosttyMouseButton,
    ) -> bool;
}
unsafe extern "C" {
    #[doc = " Set keyboard modifiers held during the event.\n\n @param event The event handle, must not be NULL\n @param mods Modifier bitmask\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_set_mods(event: GhosttyMouseEvent, mods: GhosttyMods);
}
unsafe extern "C" {
    #[doc = " Get keyboard modifiers held during the event.\n\n @param event The event handle, must not be NULL\n @return Modifier bitmask\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_get_mods(event: GhosttyMouseEvent) -> GhosttyMods;
}
unsafe extern "C" {
    #[doc = " Set the event position in surface-space pixels.\n\n @param event The event handle, must not be NULL\n @param position The position to set\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_set_position(
        event: GhosttyMouseEvent,
        position: GhosttyMousePosition,
    );
}
unsafe extern "C" {
    #[doc = " Get the event position in surface-space pixels.\n\n @param event The event handle, must not be NULL\n @return The current event position\n\n @ingroup mouse"]
    pub fn ghostty_mouse_event_get_position(event: GhosttyMouseEvent) -> GhosttyMousePosition;
}
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyMouseEncoderImpl {
    _unused: [u8; 0],
}
#[doc = " Opaque handle to a mouse encoder instance.\n\n This handle represents a mouse encoder that converts normalized\n mouse events into terminal escape sequences.\n\n @ingroup mouse"]
pub type GhosttyMouseEncoder = *mut GhosttyMouseEncoderImpl;
#[doc = " Mouse reporting disabled."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_NONE: GhosttyMouseTrackingMode = 0;
#[doc = " X10 mouse mode."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_X10: GhosttyMouseTrackingMode = 1;
#[doc = " Normal mouse mode (button press/release only)."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_NORMAL: GhosttyMouseTrackingMode = 2;
#[doc = " Button-event tracking mode."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_BUTTON: GhosttyMouseTrackingMode = 3;
#[doc = " Any-event tracking mode."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_ANY: GhosttyMouseTrackingMode = 4;
#[doc = " Any-event tracking mode."]
pub const GhosttyMouseTrackingMode_GHOSTTY_MOUSE_TRACKING_MAX_VALUE: GhosttyMouseTrackingMode =
    2147483647;
#[doc = " Mouse tracking mode.\n\n @ingroup mouse"]
pub type GhosttyMouseTrackingMode = ::std::os::raw::c_uint;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_X10: GhosttyMouseFormat = 0;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_UTF8: GhosttyMouseFormat = 1;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_SGR: GhosttyMouseFormat = 2;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_URXVT: GhosttyMouseFormat = 3;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_SGR_PIXELS: GhosttyMouseFormat = 4;
pub const GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_MAX_VALUE: GhosttyMouseFormat = 2147483647;
#[doc = " Mouse output format.\n\n @ingroup mouse"]
pub type GhosttyMouseFormat = ::std::os::raw::c_uint;
#[doc = " Mouse encoder size and geometry context.\n\n This describes the rendered terminal geometry used to convert\n surface-space positions into encoded coordinates.\n\n @ingroup mouse"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyMouseEncoderSize {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyMouseEncoderSize)."]
    pub size: usize,
    #[doc = " Full screen width in pixels."]
    pub screen_width: u32,
    #[doc = " Full screen height in pixels."]
    pub screen_height: u32,
    #[doc = " Cell width in pixels. Must be non-zero."]
    pub cell_width: u32,
    #[doc = " Cell height in pixels. Must be non-zero."]
    pub cell_height: u32,
    #[doc = " Top padding in pixels."]
    pub padding_top: u32,
    #[doc = " Bottom padding in pixels."]
    pub padding_bottom: u32,
    #[doc = " Right padding in pixels."]
    pub padding_right: u32,
    #[doc = " Left padding in pixels."]
    pub padding_left: u32,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyMouseEncoderSize"][::std::mem::size_of::<GhosttyMouseEncoderSize>() - 40usize];
    ["Alignment of GhosttyMouseEncoderSize"]
        [::std::mem::align_of::<GhosttyMouseEncoderSize>() - 8usize];
    ["Offset of field: GhosttyMouseEncoderSize::size"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, size) - 0usize];
    ["Offset of field: GhosttyMouseEncoderSize::screen_width"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, screen_width) - 8usize];
    ["Offset of field: GhosttyMouseEncoderSize::screen_height"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, screen_height) - 12usize];
    ["Offset of field: GhosttyMouseEncoderSize::cell_width"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, cell_width) - 16usize];
    ["Offset of field: GhosttyMouseEncoderSize::cell_height"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, cell_height) - 20usize];
    ["Offset of field: GhosttyMouseEncoderSize::padding_top"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, padding_top) - 24usize];
    ["Offset of field: GhosttyMouseEncoderSize::padding_bottom"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, padding_bottom) - 28usize];
    ["Offset of field: GhosttyMouseEncoderSize::padding_right"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, padding_right) - 32usize];
    ["Offset of field: GhosttyMouseEncoderSize::padding_left"]
        [::std::mem::offset_of!(GhosttyMouseEncoderSize, padding_left) - 36usize];
};
#[doc = " Mouse tracking mode (value: GhosttyMouseTrackingMode)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_EVENT: GhosttyMouseEncoderOption = 0;
#[doc = " Mouse output format (value: GhosttyMouseFormat)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_FORMAT: GhosttyMouseEncoderOption = 1;
#[doc = " Renderer size context (value: GhosttyMouseEncoderSize)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_SIZE: GhosttyMouseEncoderOption = 2;
#[doc = " Whether any mouse button is currently pressed (value: bool)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED:
    GhosttyMouseEncoderOption = 3;
#[doc = " Whether to enable motion deduplication by last cell (value: bool)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_TRACK_LAST_CELL:
    GhosttyMouseEncoderOption = 4;
#[doc = " Whether to enable motion deduplication by last cell (value: bool)."]
pub const GhosttyMouseEncoderOption_GHOSTTY_MOUSE_ENCODER_OPT_MAX_VALUE: GhosttyMouseEncoderOption =
    2147483647;
#[doc = " Mouse encoder option identifiers.\n\n These values are used with ghostty_mouse_encoder_setopt() to configure\n the behavior of the mouse encoder.\n\n @ingroup mouse"]
pub type GhosttyMouseEncoderOption = ::std::os::raw::c_uint;
unsafe extern "C" {
    #[doc = " Create a new mouse encoder instance.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param encoder Pointer to store the created encoder handle\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_new(
        allocator: *const GhosttyAllocator,
        encoder: *mut GhosttyMouseEncoder,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a mouse encoder instance.\n\n @param encoder The encoder handle to free (may be NULL)\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_free(encoder: GhosttyMouseEncoder);
}
unsafe extern "C" {
    #[doc = " Set an option on the mouse encoder.\n\n A null pointer value does nothing. It does not reset to defaults.\n\n @param encoder The encoder handle, must not be NULL\n @param option The option to set\n @param value Pointer to option value (type depends on option)\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_setopt(
        encoder: GhosttyMouseEncoder,
        option: GhosttyMouseEncoderOption,
        value: *const ::std::os::raw::c_void,
    );
}
unsafe extern "C" {
    #[doc = " Set encoder options from a terminal's current state.\n\n This sets tracking mode and output format from terminal state.\n It does not modify size or any-button state.\n\n @param encoder The encoder handle, must not be NULL\n @param terminal The terminal handle, must not be NULL\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_setopt_from_terminal(
        encoder: GhosttyMouseEncoder,
        terminal: GhosttyTerminal,
    );
}
unsafe extern "C" {
    #[doc = " Reset internal encoder state.\n\n This clears motion deduplication state (last tracked cell).\n\n @param encoder The encoder handle (may be NULL)\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_reset(encoder: GhosttyMouseEncoder);
}
unsafe extern "C" {
    #[doc = " Encode a mouse event into a terminal escape sequence.\n\n Not all mouse events produce output. In such cases this returns\n GHOSTTY_SUCCESS with out_len set to 0.\n\n If the output buffer is too small, this returns GHOSTTY_OUT_OF_SPACE\n and out_len contains the required size.\n\n @param encoder The encoder handle, must not be NULL\n @param event The mouse event to encode, must not be NULL\n @param out_buf Buffer to write encoded bytes to, or NULL to query required size\n @param out_buf_size Size of out_buf in bytes\n @param out_len Pointer to store bytes written (or required bytes on failure)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if buffer is too small,\n         or another error code\n\n @ingroup mouse"]
    pub fn ghostty_mouse_encoder_encode(
        encoder: GhosttyMouseEncoder,
        event: GhosttyMouseEvent,
        out_buf: *mut ::std::os::raw::c_char,
        out_buf_size: usize,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Check if paste data is safe to paste into the terminal.\n\n Data is considered unsafe if it contains:\n - Newlines (`\\n`) which can inject commands\n - The bracketed paste end sequence (`\\x1b[201~`) which can be used\n   to exit bracketed paste mode and inject commands\n\n This check is conservative and considers data unsafe regardless of\n current terminal state.\n\n @param data The paste data to check (must not be NULL)\n @param len The length of the data in bytes\n @return true if the data is safe to paste, false otherwise"]
    pub fn ghostty_paste_is_safe(data: *const ::std::os::raw::c_char, len: usize) -> bool;
}
unsafe extern "C" {
    #[doc = " Encode paste data for writing to the terminal pty.\n\n This function prepares paste data for terminal input by:\n - Stripping unsafe control bytes (NUL, ESC, DEL, etc.) by replacing\n   them with spaces\n - Wrapping the data in bracketed paste sequences if @p bracketed is true\n - Replacing newlines with carriage returns if @p bracketed is false\n\n The input @p data buffer is modified in place during encoding. The\n encoded result (potentially with bracketed paste prefix/suffix) is\n written to the output buffer.\n\n If the output buffer is too small, the function returns\n GHOSTTY_OUT_OF_SPACE and sets the required size in @p out_written.\n The caller can then retry with a sufficiently sized buffer.\n\n @param data The paste data to encode (modified in place, may be NULL)\n @param data_len The length of the input data in bytes\n @param bracketed Whether bracketed paste mode is active\n @param buf Output buffer to write the encoded result into (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_written On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if the buffer\n         is too small"]
    pub fn ghostty_paste_encode(
        data: *mut ::std::os::raw::c_char,
        data_len: usize,
        bracketed: bool,
        buf: *mut ::std::os::raw::c_char,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Returns the terminal display width of a Unicode codepoint in\n terminal grid cells: 0, 1, or 2.\n\n This is the same width table the terminal itself uses when laying\n out printed text, so callers can predict column layout (e.g. IME\n preedit overlays) that exactly matches what the terminal will do\n when the text is actually written to it.\n\n Semantics:\n - Returns 0 for zero-width codepoints: C0/C1 control characters,\n   nonspacing and enclosing combining marks, default-ignorable\n   codepoints (ZWJ, ZWNJ, variation selectors, etc.), and\n   surrogate codepoints.\n - Returns 2 for wide codepoints: East Asian Wide/Fullwidth\n   (including emoji with default emoji presentation) and regional\n   indicators. Width is clamped to 2 (e.g. the three-em dash).\n - Returns 1 for everything else, including invalid codepoints\n   beyond U+10FFFF (this function is total; it never fails).\n\n This operates on a single codepoint only and therefore cannot account\n for grapheme-cluster-level width rules (VS16 emoji presentation,\n combining sequences, etc.). For cluster-accurate widths, use\n ghostty_unicode_grapheme_width(). Summing per-codepoint widths is only\n correct when mode 2027 (grapheme clustering) is disabled.\n\n This function is pure, allocates nothing, and is thread-safe.\n\n @param cp The Unicode codepoint to measure\n @return Display width in cells: 0, 1, or 2"]
    pub fn ghostty_unicode_codepoint_width(cp: u32) -> u8;
}
unsafe extern "C" {
    #[doc = " Measures the terminal display width of the first grapheme cluster in a\n sequence of Unicode codepoints.\n\n This uses the exact same grapheme segmentation and cluster width rules\n the terminal itself uses when printing text with grapheme clustering\n enabled (mode 2027), so callers can predict column layout (e.g. IME\n preedit overlays) that exactly matches what the terminal will do when\n the text is actually written to it. Unlike\n ghostty_unicode_codepoint_width(), this accounts for cluster-level\n rules: emoji variation selectors, ZWJ sequences, combining marks, and\n skin tone modifiers.\n\n Reads codepoints from cps until the terminal would consider the\n grapheme cluster complete, stores the cluster's total width in cells\n (0, 1, or 2) into width (which may be NULL if only segmentation is\n desired), and returns the number of codepoints consumed. Returns 0 if\n and only if len is 0; otherwise consumes at least one codepoint. Measure\n a whole string by calling in a loop:\n\n @code\n size_t total = 0;\n for (size_t i = 0; i < len;) {\n   uint8_t width;\n   i += ghostty_unicode_grapheme_width(cps + i, len - i, &width);\n   total += width;\n }\n @endcode\n\n This is not a streaming API. The provided sequence must contain a\n complete first grapheme cluster, or the logical end of the string. If\n input arrives in chunks, keep buffering while this function consumes all\n available codepoints (return value == len) and the stream may still\n continue; a later codepoint could still extend the cluster and change\n its width.\n\n Width semantics, matching the terminal with mode 2027 enabled:\n - The cluster starts at the width of its first codepoint, as returned by\n   ghostty_unicode_codepoint_width().\n - VS16 (U+FE0F) forces the cluster wide (2) and VS15 (U+FE0E) forces it\n   narrow (1), but only when the immediately preceding codepoint in the\n   cluster is a valid emoji variation sequence base (per Unicode\n   emoji-variation-sequences.txt). Invalid variation selectors are\n   ignored entirely.\n - Any other continuation codepoint that contributes to grapheme width\n   forces the cluster wide (2). Note this means cluster width is NOT the\n   maximum of per-codepoint widths: some continuation marks have narrow\n   codepoint width yet still widen the cluster.\n\n Mode dependence: this models mode 2027 (grapheme clustering) enabled,\n which is Ghostty's recommended configuration. When mode 2027 is\n disabled, clusters never combine and variation selectors never change\n width; predict layout in that case by summing\n ghostty_unicode_codepoint_width() over each codepoint instead.\n\n Edge cases:\n - Codepoints beyond U+10FFFF consume one codepoint, have width 1, and\n   are always cluster boundaries. This function is total; it never fails.\n - Control characters (C0/C1, CR, LF) are never printed through the\n   terminal's text path; passing them here returns an unspecified (but\n   stable and bounded) result.\n - A cluster whose first codepoint is zero-width (e.g. a lone combining\n   mark) is malformed at a cell start; the terminal may attach it to\n   earlier screen content. This function reports the fold result for the\n   sequence in isolation (typically 0).\n\n This function is pure, allocates nothing, and is thread-safe.\n\n @param cps Pointer to codepoints (may be NULL only when len is 0)\n @param len Number of codepoints available\n @param width Out: cluster display width in cells (0-2); may be NULL\n @return Number of codepoints in the first grapheme cluster"]
    pub fn ghostty_unicode_grapheme_width(cps: *const u32, len: usize, width: *mut u8) -> usize;
}
