#[doc = " A synchronous request to read clipboard contents.\n\n This is a sized struct. The callback must only access fields present in the\n size reported by `size`. The request is borrowed and valid only for the\n callback duration.\n\n The read is answered by calling `reply` with this request and a\n GhosttyClipboardReadReply. This must happen before the callback returns;\n the request is invalid afterwards. Calling `reply` more than once is\n ignored. Returning without replying answers the program with an empty\n clipboard (OSC 52) or EPERM (OSC 5522).\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyClipboardRead {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Clipboard to read."]
    pub location: GhosttyClipboardLocation,
    #[doc = " Borrowed array of the MIME types the program wants, in order of\n preference. Protocols that only carry text (OSC 52) request\n \"text/plain\". NULL when mimes_len is zero."]
    pub mimes: *const GhosttyString,
    #[doc = " Number of entries in mimes."]
    pub mimes_len: usize,
    #[doc = " True if the program also wants the list of MIME types available on the\n clipboard, delivered through GhosttyClipboardReadReply::available."]
    pub list: bool,
    #[doc = " Name of the requesting program for permission prompts, if the protocol\n carries one. Empty otherwise."]
    pub name: GhosttyString,
    #[doc = " True if the terminal already holds a session grant for this request\n (kitty clipboard protocol passwords). The embedder should skip any\n permission prompt and serve the read.\n\n Always false when mimes_len is zero: such a request is served\n without a prompt (see the callback docs), so the terminal never\n consults grants for it and a one-time password is preserved for\n the follow-up data read."]
    pub granted: bool,
    #[doc = " True if the program supplied a session password, so the embedder may\n offer to remember the user's decision through\n GhosttyClipboardReadReply::remember. When false, remember is ignored."]
    pub can_remember: bool,
    #[doc = " Terminal-owned reply state. Do not access."]
    pub ctx: *const ::std::os::raw::c_void,
    #[doc = " Answer the read; see the struct documentation."]
    pub reply: GhosttyClipboardReadReplyFn,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyClipboardRead"][::std::mem::size_of::<GhosttyClipboardRead>() - 80usize];
    ["Alignment of GhosttyClipboardRead"][::std::mem::align_of::<GhosttyClipboardRead>() - 8usize];
    ["Offset of field: GhosttyClipboardRead::size"]
        [::std::mem::offset_of!(GhosttyClipboardRead, size) - 0usize];
    ["Offset of field: GhosttyClipboardRead::location"]
        [::std::mem::offset_of!(GhosttyClipboardRead, location) - 8usize];
    ["Offset of field: GhosttyClipboardRead::mimes"]
        [::std::mem::offset_of!(GhosttyClipboardRead, mimes) - 16usize];
    ["Offset of field: GhosttyClipboardRead::mimes_len"]
        [::std::mem::offset_of!(GhosttyClipboardRead, mimes_len) - 24usize];
    ["Offset of field: GhosttyClipboardRead::list"]
        [::std::mem::offset_of!(GhosttyClipboardRead, list) - 32usize];
    ["Offset of field: GhosttyClipboardRead::name"]
        [::std::mem::offset_of!(GhosttyClipboardRead, name) - 40usize];
    ["Offset of field: GhosttyClipboardRead::granted"]
        [::std::mem::offset_of!(GhosttyClipboardRead, granted) - 56usize];
    ["Offset of field: GhosttyClipboardRead::can_remember"]
        [::std::mem::offset_of!(GhosttyClipboardRead, can_remember) - 57usize];
    ["Offset of field: GhosttyClipboardRead::ctx"]
        [::std::mem::offset_of!(GhosttyClipboardRead, ctx) - 64usize];
    ["Offset of field: GhosttyClipboardRead::reply"]
        [::std::mem::offset_of!(GhosttyClipboardRead, reply) - 72usize];
};
impl Default for GhosttyClipboardRead {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Callback function type for clipboard_read.\n\n Called synchronously when the running program requests clipboard contents\n via OSC 52 with a \"?\" payload or a Kitty clipboard (OSC 5522) read.\n Answering lets the program read the user's clipboard, so the embedder is\n expected to mediate consent. Because the read is synchronous, an embedder\n that needs to ask the user must block (for example by running a modal\n prompt) until it has an answer; the VT stream waits until the callback\n returns.\n\n Answer by calling `read->reply(read, &reply)` before returning. See\n GhosttyClipboardRead for the full contract.\n\n OSC 5522 requests carry the program's MIME list, name, and password grant\n state; a reply that sets `remember` records a session grant so later\n requests with the same password arrive with `granted` set. Kitty itself\n serves a request for only the targets listing (`list` with no `mimes`)\n without prompting, and embedders are expected to do the same; the\n terminal never consults grants for such requests (`granted` is false\n and one-time passwords are not consumed).\n\n Installing this callback also enables Kitty paste events (mode 5522):\n ghostty_terminal_paste() sends the program an event instead of the text,\n and the program's follow-up read arrives here with `granted` set since\n the user already pasted. See ghostty_terminal_paste().\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param read Borrowed clipboard read request\n\n @ingroup terminal"]
pub type GhosttyTerminalClipboardReadFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        read: *const GhosttyClipboardRead,
    ),
>;
#[doc = " A request to show a desktop notification.\n\n This is a sized struct. The callback must only access fields present in the\n size reported by `size`. Both strings are borrowed and valid only for the\n duration of the callback.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalDesktopNotification {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Notification title, or an empty string when the protocol omits it."]
    pub title: GhosttyString,
    #[doc = " Notification body."]
    pub body: GhosttyString,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalDesktopNotification"]
        [::std::mem::size_of::<GhosttyTerminalDesktopNotification>() - 40usize];
    ["Alignment of GhosttyTerminalDesktopNotification"]
        [::std::mem::align_of::<GhosttyTerminalDesktopNotification>() - 8usize];
    ["Offset of field: GhosttyTerminalDesktopNotification::size"]
        [::std::mem::offset_of!(GhosttyTerminalDesktopNotification, size) - 0usize];
    ["Offset of field: GhosttyTerminalDesktopNotification::title"]
        [::std::mem::offset_of!(GhosttyTerminalDesktopNotification, title) - 8usize];
    ["Offset of field: GhosttyTerminalDesktopNotification::body"]
        [::std::mem::offset_of!(GhosttyTerminalDesktopNotification, body) - 24usize];
};
impl Default for GhosttyTerminalDesktopNotification {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Callback function type for desktop notifications.\n\n Called synchronously when the terminal receives OSC 9 or OSC 777.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param notification Borrowed desktop notification request\n\n @ingroup terminal"]
pub type GhosttyTerminalDesktopNotificationFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        notification: *const GhosttyTerminalDesktopNotification,
    ),
>;
#[doc = " Remove any visible progress indication."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_REMOVE:
    GhosttyTerminalProgressState = 0;
#[doc = " Show determinate progress."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_SET:
    GhosttyTerminalProgressState = 1;
#[doc = " Show a failed progress state."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_ERROR:
    GhosttyTerminalProgressState = 2;
#[doc = " Show indeterminate progress."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_INDETERMINATE:
    GhosttyTerminalProgressState = 3;
#[doc = " Show paused progress."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_PAUSE:
    GhosttyTerminalProgressState = 4;
#[doc = " Show paused progress."]
pub const GhosttyTerminalProgressState_GHOSTTY_TERMINAL_PROGRESS_STATE_MAX_VALUE:
    GhosttyTerminalProgressState = 2147483647;
#[doc = " State of a terminal progress report.\n\n @ingroup terminal"]
pub type GhosttyTerminalProgressState = ::std::os::raw::c_int;
#[doc = " A progress report emitted by the running program.\n\n This is a sized struct. The callback must only access fields present in the\n size reported by `size`.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyTerminalProgressReport {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " Literal progress state reported by the running program."]
    pub state: GhosttyTerminalProgressState,
    #[doc = " Progress percentage from 0 through 100, or -1 when omitted."]
    pub progress: i8,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalProgressReport"]
        [::std::mem::size_of::<GhosttyTerminalProgressReport>() - 16usize];
    ["Alignment of GhosttyTerminalProgressReport"]
        [::std::mem::align_of::<GhosttyTerminalProgressReport>() - 8usize];
    ["Offset of field: GhosttyTerminalProgressReport::size"]
        [::std::mem::offset_of!(GhosttyTerminalProgressReport, size) - 0usize];
    ["Offset of field: GhosttyTerminalProgressReport::state"]
        [::std::mem::offset_of!(GhosttyTerminalProgressReport, state) - 8usize];
    ["Offset of field: GhosttyTerminalProgressReport::progress"]
        [::std::mem::offset_of!(GhosttyTerminalProgressReport, progress) - 12usize];
};
impl Default for GhosttyTerminalProgressReport {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
#[doc = " Callback function type for progress reports.\n\n Called synchronously when the terminal receives OSC 9;4.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param report Borrowed progress report\n\n @ingroup terminal"]
pub type GhosttyTerminalProgressReportFn = ::std::option::Option<
    unsafe extern "C" fn(
        terminal: GhosttyTerminal,
        userdata: *mut ::std::os::raw::c_void,
        report: *const GhosttyTerminalProgressReport,
    ),
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
#[doc = " Callback function type for terminal size reports.\n\n Called in response to XTWINOPS size queries (CSI 14/16/18 t) and when VT\n input enables in-band size reports (mode 2048).\n Return true and fill *out_size with the current terminal geometry,\n or return false to suppress the report.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param[out] out_size Pointer to store the terminal size information\n @return true if size was filled, false to suppress the XTWINOPS response or\n mode 2048 report\n\n @ingroup terminal"]
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
#[doc = " Callback function type for write_pty.\n\n Called when the terminal needs to write data back to the pty, for\n example in response to a device status report, mode query, or VT-driven\n mode 2048 enable. The data is only valid for the duration of the call;\n callers must copy it if it needs to persist.\n\n @param terminal The terminal handle\n @param userdata The userdata pointer set via GHOSTTY_TERMINAL_OPT_USERDATA\n @param data Pointer to the response bytes\n @param len Length of the response in bytes\n\n @ingroup terminal"]
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
#[doc = " A terminal mode and boolean value used for mode configuration and queries.\n\n For GHOSTTY_TERMINAL_DATA_MODE, initialize `mode` before calling\n ghostty_terminal_get(). On success, `value` contains the current mode value.\n\n This struct has a frozen layout and will not gain fields in future versions.\n\n @ingroup terminal"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyTerminalModeConfig {
    #[doc = " Mode to configure or query."]
    pub mode: GhosttyMode,
    #[doc = " Value to set, or the current value returned by a query."]
    pub value: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyTerminalModeConfig"]
        [::std::mem::size_of::<GhosttyTerminalModeConfig>() - 4usize];
    ["Alignment of GhosttyTerminalModeConfig"]
        [::std::mem::align_of::<GhosttyTerminalModeConfig>() - 2usize];
    ["Offset of field: GhosttyTerminalModeConfig::mode"]
        [::std::mem::offset_of!(GhosttyTerminalModeConfig, mode) - 0usize];
    ["Offset of field: GhosttyTerminalModeConfig::value"]
        [::std::mem::offset_of!(GhosttyTerminalModeConfig, value) - 2usize];
};
#[doc = " Opaque userdata pointer passed to all callbacks.\n\n Input type: void*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_USERDATA: GhosttyTerminalOption = 0;
#[doc = " Callback invoked when the terminal needs to write data back\n to the pty (e.g. in response to a DECRQM query, device status\n report, or VT-driven mode 2048 enable). Set to NULL to ignore such\n sequences.\n\n Input type: GhosttyTerminalWritePtyFn"]
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
#[doc = " Enable Kitty image loading via the temporary file medium, restricted to\n the provided directory. The string data is copied into the terminal.\n\n A NULL value pointer disables the temporary file medium. Has no effect\n when Kitty graphics are disabled at build time.\n\n Input type: GhosttyString*"]
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
#[doc = " Callback invoked when the running program performs a clipboard write.\n OSC 52, iTerm2 OSC 1337 Copy, and Kitty clipboard (OSC 5522) writes\n are normalized to an atomic set of decoded MIME representations. Set\n to NULL to ignore clipboard writes (Kitty clipboard writes are then\n refused with ENOSYS). Clipboard read requests are delivered to\n GHOSTTY_TERMINAL_OPT_CLIPBOARD_READ instead.\n\n Input type: GhosttyTerminalClipboardWriteFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE: GhosttyTerminalOption = 26;
#[doc = " Set the maximum scrollback allocation in bytes.\n\n This is an estimate. Internally, libghostty only prunes bytes up\n to a \"page\"-granularity. A page is the minimum allocated unit of\n grid space within Ghostty. A page at the time of writing these docs\n is about 400KB, so the byte limit will be within this delta.\n\n This works alongside the line limit configuration. If both are set,\n the first-reached limit is used first. Both limits are dependent\n on external state (byte limit can be reached with less lines if\n more styles are used for example, line limit can be reached with\n a narrower terminal viewport). So, they are useful together.\n\n Lowering the limit immediately removes eligible complete historical\n pages. A value of zero disables scrollback and erases retained history.\n A NULL value pointer removes the byte limit.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES: GhosttyTerminalOption =
    27;
#[doc = " Set the maximum number of physical lines retained in scrollback.\n\n This is an estimate. Internally, libghostty only prunes lines up\n to a \"page\"-granularity. A page is the minimum allocated unit of\n grid space within Ghostty. As a result, the actual available scrollback\n lines will almost always be higher than configured. The magnitude\n of the difference depends on the number of used styles, graphemes, etc.\n since the row-count in a page is dynamic based on that. In general,\n it ranges from dozens to a hundred or so lines.\n\n This works alongside the line limit configuration. If both are set,\n the first-reached limit is used first. Both limits are dependent\n on external state (byte limit can be reached with less lines if\n more styles are used for example, line limit can be reached with\n a narrower terminal viewport). So, they are useful together.\n\n Lowering the limit immediately removes eligible complete historical\n pages. A NULL value pointer removes the line limit.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES: GhosttyTerminalOption =
    28;
#[doc = " Callback invoked when the running program requests a desktop\n notification via OSC 9 or OSC 777. Set to NULL to ignore desktop\n notification requests.\n\n Input type: GhosttyTerminalDesktopNotificationFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_DESKTOP_NOTIFICATION: GhosttyTerminalOption =
    29;
#[doc = " Callback invoked when the running program reports progress via OSC 9;4.\n Set to NULL to ignore progress reports.\n\n Input type: GhosttyTerminalProgressReportFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_PROGRESS_REPORT: GhosttyTerminalOption = 30;
#[doc = " Set the maximum number of replay-safe VT continuation bytes retained.\n\n Continuation bytes reconstruct an escape sequence or UTF-8 codepoint\n which was unfinished at the end of the most recent\n VT write call. They are used automatically by terminal snapshots and may\n also be exported directly with the continuation APIs.\n\n Tracking is disabled by default. A nonzero value enables tracking and\n sets its byte limit. Passing NULL or a pointer to zero disables tracking.\n Lowering the limit below an already-retained\n continuation, or enabling tracking while the parser is already\n unfinished, makes the current continuation unavailable because earlier\n bytes cannot be reconstructed. Tracking recovers automatically after a\n later write reaches the ground state or contains a fresh replay start.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES: GhosttyTerminalOption =
    31;
#[doc = " Enable window title reports in response to CSI 21 t.\n\n This is disabled by default because a running program can set a title and\n query it back into the pty input stream, potentially injecting commands\n that execute after user interaction. Passing NULL or a pointer to false\n disables title reporting.\n\n Input type: bool*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_TITLE_REPORT: GhosttyTerminalOption = 32;
#[doc = " Set the reset default for a terminal mode.\n\n This unconditionally updates both the current value and the value restored\n by a full terminal reset (RIS).\n\n Some recognized modes represent transitions or mirror additional terminal\n state and cannot safely be configured as reset defaults. Those modes return\n GHOSTTY_INVALID_VALUE. A NULL value pointer also returns\n GHOSTTY_INVALID_VALUE.\n\n Input type: GhosttyTerminalModeConfig*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_MODE_DEFAULT: GhosttyTerminalOption = 33;
#[doc = " Set the current value of a terminal mode.\n\n This does not change the value restored by a full terminal reset (RIS).\n A NULL value pointer or unknown mode returns GHOSTTY_INVALID_VALUE.\n\n Input type: GhosttyTerminalModeConfig*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_MODE: GhosttyTerminalOption = 34;
#[doc = " Callback invoked for unsupported terminal sequence identifiers. Set to\n NULL to ignore unsupported sequences. Capture must also be enabled with\n GHOSTTY_TERMINAL_OPT_UNKNOWN_MAX_BYTES.\n\n Input type: GhosttyTerminalUnknownSequenceFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_UNKNOWN_SEQUENCE: GhosttyTerminalOption = 35;
#[doc = " Set the maximum content bytes retained for each unsupported terminal\n sequence. A NULL value pointer or zero disables capture and prevents\n unknown-sequence callbacks.\n\n When this limit is hit, the unknown sequence callback will still\n be invoked but `truncated` will be set to true.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_UNKNOWN_MAX_BYTES: GhosttyTerminalOption = 36;
#[doc = " Set the name of the terminfo entry this terminal runs as, reported\n in response to an XTGETTCAP query for \"TN\" (e.g. \"xterm-256color\").\n\n The string data is copied into the terminal. A NULL value pointer\n clears the name (equivalent to setting an empty string). A name\n longer than 128 bytes returns GHOSTTY_INVALID_VALUE.\n\n If this is unset then we don't report anything for an XTGETTCAP\n TN query, because we don't know what the embedding terminal around\n libghostty is advertising itself as.\n\n Input type: GhosttyString*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_TERMINFO_NAME: GhosttyTerminalOption = 37;
#[doc = " Callback invoked when the running program requests clipboard contents\n via OSC 52 with a \"?\" payload or a Kitty clipboard (OSC 5522) read. The\n read is synchronous and must be answered before the callback returns.\n Set to NULL (the default) to ignore OSC 52 read requests and refuse\n OSC 5522 reads with EPERM.\n\n Input type: GhosttyTerminalClipboardReadFn"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CLIPBOARD_READ: GhosttyTerminalOption = 38;
#[doc = " Set the maximum total decoded bytes a single Kitty clipboard protocol\n (OSC 5522) write transaction may accumulate. The limit is captured\n when a transaction begins; an in-flight transaction keeps the limit\n it started with.\n\n Data beyond the limit fails the whole transaction with EFBIG. The\n transaction is discarded, later write-related packets are ignored\n until a new write begins, and nothing reaches the clipboard write\n callback.\n\n Transactions are buffered in memory, so this limit bounds how much\n memory a single write can make the terminal allocate. Pass SIZE_MAX\n to remove the limit. A NULL value pointer reverts to the built-in\n default of 64MiB, the minimum required by the protocol.\n\n This limit doesn't apply to OSC 52 writes, which are bounded by the\n maximum length of an escape sequence instead.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE_MAX_BYTES:
    GhosttyTerminalOption = 39;
#[doc = " Set the maximum total decoded bytes a single Kitty clipboard protocol\n (OSC 5522) write transaction may accumulate. The limit is captured\n when a transaction begins; an in-flight transaction keeps the limit\n it started with.\n\n Data beyond the limit fails the whole transaction with EFBIG. The\n transaction is discarded, later write-related packets are ignored\n until a new write begins, and nothing reaches the clipboard write\n callback.\n\n Transactions are buffered in memory, so this limit bounds how much\n memory a single write can make the terminal allocate. Pass SIZE_MAX\n to remove the limit. A NULL value pointer reverts to the built-in\n default of 64MiB, the minimum required by the protocol.\n\n This limit doesn't apply to OSC 52 writes, which are bounded by the\n maximum length of an escape sequence instead.\n\n Input type: size_t*"]
pub const GhosttyTerminalOption_GHOSTTY_TERMINAL_OPT_MAX_VALUE: GhosttyTerminalOption = 2147483647;
#[doc = " Terminal option identifiers.\n\n These values are used with ghostty_terminal_set() to configure\n terminal callbacks and associated state.\n\n @ingroup terminal"]
pub type GhosttyTerminalOption = ::std::os::raw::c_int;
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
#[doc = " The terminal title as set by escape sequences (e.g. OSC 0/2).\n\n Returns a borrowed string. The pointer is valid until the next mutating\n terminal call. An empty string (len=0) is returned when no title has been\n set.\n\n Output type: GhosttyString *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_TITLE: GhosttyTerminalData = 12;
#[doc = " The terminal's current working directory as set by escape sequences\n (e.g. OSC 7).\n\n Returns a borrowed string. The pointer is valid until the next mutating\n terminal call. An empty string (len=0) is returned when no pwd has been\n set.\n\n Output type: GhosttyString *"]
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
#[doc = " The directory allowed for Kitty image loading via the temporary file\n medium on the active screen. The string is empty when the medium is\n disabled.\n\n Returns GHOSTTY_NO_VALUE when Kitty graphics are disabled at build time.\n\n Output type: GhosttyString *"]
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
#[doc = " Whether VT processing encountered a non-gracefully handled error that may\n have prevented a terminal-owned semantic update.\n\n Processing remains best-effort, and ghostty_terminal_reset() does not\n clear it. Gracefully handled protocol failures, configured limits,\n malformed or unsupported input, and failures limited to external effects\n or query responses do not set it.\n\n This can't currently be unset. This is purely informational to consumers\n if there was some error that happened at some point during VT processing.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_VT_PROCESSING_ERROR: GhosttyTerminalData = 33;
#[doc = " The configured maximum scrollback allocation in bytes.\n\n This always reports the primary screen's configured value, including\n while an alternate screen is active. Returns GHOSTTY_NO_VALUE when the\n configured byte limit is unlimited.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBACK_MAX_BYTES: GhosttyTerminalData = 34;
#[doc = " The configured maximum number of physical scrollback lines.\n\n This always reports the primary screen's configured value, including\n while an alternate screen is active. Returns GHOSTTY_NO_VALUE when the\n configured line limit is unlimited.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_SCROLLBACK_MAX_LINES: GhosttyTerminalData = 35;
#[doc = " The configured maximum retained VT continuation size in bytes.\n\n A value of zero means continuation tracking is disabled. This reports the\n configured limit even when a current unfinished continuation is\n temporarily unavailable.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CONTINUATION_MAX_BYTES: GhosttyTerminalData =
    36;
#[doc = " Get the current value of a terminal mode.\n\n The caller must initialize the `mode` field. On success, the `value` field\n is updated with the current value. A NULL pointer or unknown mode returns\n GHOSTTY_INVALID_VALUE.\n\n Input/output type: GhosttyTerminalModeConfig *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_MODE: GhosttyTerminalData = 37;
#[doc = " Whether VT processing is at ground.\n\n Ground is when the stream isn't in the middle of any type of sequence:\n UTF-8, ESC, CSI, OSC, etc. It is the stateless point of the stream.\n\n This is useful to know because it is a point at which you can\n safely insert out-of-band VT sequences. For example, while reading\n from a pty if you want to make your own changes, you can wait until\n the pty input reaches ground, then write yours.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_VT_GROUND: GhosttyTerminalData = 38;
#[doc = " Whether the cursor is currently at a semantic shell prompt or input area.\n\n This depends on semantic prompt markers such as OSC 133. Returns false\n when semantic prompt information is unavailable or the alternate screen\n is active.\n\n Output type: bool *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CURSOR_AT_PROMPT: GhosttyTerminalData = 39;
#[doc = " The configured maximum decoded bytes per Kitty clipboard protocol\n (OSC 5522) write transaction. See\n GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE_MAX_BYTES.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_CLIPBOARD_WRITE_MAX_BYTES: GhosttyTerminalData =
    40;
#[doc = " The configured maximum decoded bytes per Kitty clipboard protocol\n (OSC 5522) write transaction. See\n GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE_MAX_BYTES.\n\n Output type: size_t *"]
pub const GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_MAX_VALUE: GhosttyTerminalData = 2147483647;
#[doc = " Terminal data types.\n\n These values specify what type of data to extract from a terminal\n using `ghostty_terminal_get`.\n\n @ingroup terminal"]
pub type GhosttyTerminalData = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Create a new terminal instance.\n\n The terminal starts with various reasonable defaults e.g. around\n scrollback limits. Use ghostty_terminal_set() to change any options\n prior to using the terminal.\n\n @param allocator Pointer to allocator, or NULL to use the default allocator\n @param terminal Pointer to store the created terminal handle\n @param cols Terminal width in cells (must be greater than zero)\n @param rows Terminal height in cells (must be greater than zero)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup terminal"]
    pub fn ghostty_terminal_new(
        allocator: *const GhosttyAllocator,
        terminal: *mut GhosttyTerminal,
        cols: u16,
        rows: u16,
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
    #[doc = " Set an option on the terminal.\n\n Configures terminal callbacks and associated state such as the\n write_pty callback and userdata pointer. The value is passed\n directly for pointer types (callbacks, userdata) or as a pointer\n to the value for non-pointer types (e.g. GhosttyString*).\n The behavior of a NULL value is specific to each option and is\n documented by the corresponding GhosttyTerminalOption value.\n\n Callbacks are invoked synchronously during VT writes. Callbacks must not\n call ghostty_terminal_vt_write() or\n ghostty_terminal_vt_write_until_ground() on the same terminal\n (no reentrancy).\n\n @param terminal The terminal handle (may be NULL, in which case this is a no-op)\n @param option The option to set\n @param value Pointer to the value to set (type depends on the option),\n              or NULL to clear the option\n\n @ingroup terminal"]
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
    #[doc = " Write VT-encoded data, but only the shortest prefix needed to reach ground.\n\n Ground is when the stream isn't in the middle of any type of sequence:\n UTF-8, ESC, CSI, OSC, etc. It is the stateless point of the stream.\n\n This is useful to know because it is a point at which you can\n safely insert out-of-band VT sequences. For example, while reading\n from a pty if you want to make your own changes, you can wait until\n the pty input reaches ground, then write yours.\n\n If the stream is already at ground then this consumes nothing and returns\n GHOSTTY_SUCCESS. On success, out_consumed is the number of bytes consumed\n before reaching ground, including the byte that reaches it.\n GHOSTTY_NO_VALUE means the full slice was consumed without reaching ground.\n\n @param terminal The terminal handle (must not be NULL)\n @param data Pointer to the data to write, or NULL when len is zero\n @param len Length of the data in bytes\n @param[out] out_consumed Number of bytes consumed (must not be NULL)\n @return GHOSTTY_SUCCESS if ground was reached, GHOSTTY_NO_VALUE if all input\n         was consumed without reaching ground, or GHOSTTY_INVALID_VALUE if\n         an argument is invalid\n\n @ingroup terminal"]
    pub fn ghostty_terminal_vt_write_until_ground(
        terminal: GhosttyTerminal,
        data: *const u8,
        len: usize,
        out_consumed: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Write the terminal's replay-safe VT continuation to a callback writer.\n\n The continuation is the exact byte suffix needed to reconstruct unfinished\n VT parser or UTF-8 decoder state in an equivalent terminal. It is empty\n when the stream is at ground. The callback is invoked synchronously and\n may be called more than once. It must not call terminal APIs with the same\n terminal handle.\n\n Continuation tracking must have been enabled by setting\n GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to a nonzero value before the\n input that produced the continuation was written.\n\n The caller must serialize this operation with both VT write functions and\n all other access to the same terminal.\n\n @param terminal Terminal to read from (must not be NULL)\n @param writer Destination writer whose write callback must not be NULL\n @return GHOSTTY_SUCCESS on success, GHOSTTY_IO_ERROR if the callback rejects\n         a write, GHOSTTY_LIMIT_EXCEEDED if output accounting overflows, or\n         GHOSTTY_INVALID_VALUE if an argument is invalid, tracking is\n         disabled, or the current continuation is unavailable\n\n @ingroup terminal"]
    pub fn ghostty_terminal_continuation_write(
        terminal: GhosttyTerminal,
        writer: GhosttyWriter,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Copy the terminal's replay-safe VT continuation into a caller buffer.\n\n Pass NULL for buf with buf_len zero to query the required size. A size query\n returns GHOSTTY_OUT_OF_SPACE and stores the required size in out_written,\n including zero when the stream is at ground. If a non-NULL buffer is too\n small, the function has the same result and reports the full required size.\n Continuation tracking must have been enabled by setting\n GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to a nonzero value before the\n input that produced the continuation was written.\n\n The caller must serialize this operation with all other access to the same\n terminal.\n\n @param terminal Terminal to read from (must not be NULL)\n @param buf Destination buffer, or NULL when buf_len is zero\n @param buf_len Destination buffer capacity in bytes\n @param[out] out_written Bytes written, or required size on\n             GHOSTTY_OUT_OF_SPACE (must not be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE for a size query or\n         insufficient buffer, or GHOSTTY_INVALID_VALUE if an argument is\n         invalid, tracking is disabled, or the current continuation is\n         unavailable\n\n @ingroup terminal"]
    pub fn ghostty_terminal_continuation_buf(
        terminal: GhosttyTerminal,
        buf: *mut u8,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Return an allocated copy of the terminal's replay-safe VT continuation.\n\n The returned bytes are allocated with allocator, or the default allocator\n when allocator is NULL. The caller must release them with ghostty_free(),\n passing the same allocator and returned length. An empty continuation is a\n successful result with *out_ptr set to NULL and *out_len set to zero,\n which can also be passed to ghostty_free().\n Continuation tracking must have been enabled by setting\n GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to a nonzero value before the\n input that produced the continuation was written.\n\n The caller must serialize this operation with all other access to the same\n terminal.\n\n @param terminal Terminal to read from (must not be NULL)\n @param allocator Allocator for the output, or NULL for the default allocator\n @param[out] out_ptr Allocated continuation bytes (must not be NULL)\n @param[out] out_len Number of continuation bytes (must not be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_MEMORY on allocation\n         failure, or GHOSTTY_INVALID_VALUE if an argument is invalid,\n         tracking is disabled, or the current continuation is unavailable\n\n @ingroup terminal"]
    pub fn ghostty_terminal_continuation_alloc(
        terminal: GhosttyTerminal,
        allocator: *const GhosttyAllocator,
        out_ptr: *mut *mut u8,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Scroll the terminal viewport.\n\n Scrolls the terminal's viewport according to the given behavior.\n When using GHOSTTY_SCROLL_VIEWPORT_DELTA, set the delta field in\n the value union to specify the number of rows to scroll (negative\n for up, positive for down). When using GHOSTTY_SCROLL_VIEWPORT_ROW,\n set the row field to the absolute row offset from the top of the\n scrollable area (the same row space as the offset field of\n GhosttyTerminalScrollbar). For other behaviors, the value is ignored.\n\n @param terminal The terminal handle (may be NULL, in which case this is a no-op)\n @param behavior The scroll behavior as a tagged union\n\n @ingroup terminal"]
    pub fn ghostty_terminal_scroll_viewport(
        terminal: GhosttyTerminal,
        behavior: GhosttyTerminalScrollViewport,
    );
}
unsafe extern "C" {
    #[doc = " Return the current compression activity token.\n\n The token is opaque and only equality comparisons are meaningful. An\n embedding application should cache it and restart its compression idle\n delay whenever the value changes. The value may wrap and changes in either\n direction have the same meaning.\n\n This function only observes terminal state. It does not perform or schedule\n compression.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param[out] out_activity Receives the current activity token\n @return GHOSTTY_SUCCESS on success, or GHOSTTY_INVALID_VALUE if an argument\n         is NULL\n\n @ingroup terminal"]
    pub fn ghostty_terminal_compression_activity(
        terminal: GhosttyTerminal,
        out_activity: *mut u64,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Compress eligible terminal scrollback.\n\n Incremental mode performs bounded work suitable for an idle callback. A\n pending result means the application should invoke another step while the\n terminal remains idle. A complete result means no continuation is needed\n until ghostty_terminal_compression_activity() changes. Full mode performs\n one synchronous scan and can stall on large scrollback buffers.\n\n Compression is opportunistic. Complete means the pass has finished, not\n that every page was compressed: pages may be unprofitable or encounter an\n allocation or reclamation failure. Compression changes only the terminal's\n storage representation and never its logical contents or scrollback limit.\n Accessing compressed history restores it transparently.\n\n This function is not thread-safe with other operations on the same\n terminal. The caller must serialize it with writes, rendering, searches,\n and other terminal access.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param mode The amount of compression work to perform\n @param[out] out_result Receives the compression scheduling result\n @return GHOSTTY_SUCCESS on success, or GHOSTTY_INVALID_VALUE if an argument\n         or mode is invalid\n\n @ingroup terminal"]
    pub fn ghostty_terminal_compress(
        terminal: GhosttyTerminal,
        mode: GhosttyTerminalCompressionMode,
        out_result: *mut GhosttyTerminalCompressionResult,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get data from a terminal instance.\n\n Extracts typed data from the given terminal based on the specified\n data type. The output pointer must be of the appropriate type for the\n requested data kind. Valid data types and output types are documented\n in the `GhosttyTerminalData` enum.\n\n @param terminal The terminal handle (may be NULL)\n @param data The type of data to extract\n @param out Pointer to store the extracted data (type depends on data parameter)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal\n         is NULL or the data type is invalid\n\n @ingroup terminal"]
    pub fn ghostty_terminal_get(
        terminal: GhosttyTerminal,
        data: GhosttyTerminalData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple data fields from a terminal in a single call.\n\n This is an optimization over calling ghostty_terminal_get()\n repeatedly, particularly useful in environments with high per-call\n overhead such as FFI or Cgo.\n\n Each element in the keys array specifies a data kind, and the\n corresponding element in the values array receives the result.\n The type of each values[i] pointer must match the output type\n documented for keys[i].\n\n Processing stops at the first error; on success out_written\n is set to count, on error it is set to the index of the\n failing key (i.e. the number of values successfully written).\n\n @param terminal The terminal handle (may be NULL)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers (types must match each key's\n               documented output type)\n @param[out] out_written On return, receives the number of values\n             successfully written (may be NULL)\n @return GHOSTTY_SUCCESS if all queries succeed\n\n @ingroup terminal"]
    pub fn ghostty_terminal_get_multi(
        terminal: GhosttyTerminal,
        count: usize,
        keys: *const GhosttyTerminalData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Resolve a point in the terminal grid to a grid reference.\n\n Resolves the given point (which can be in active, viewport, screen,\n or history coordinates) to a grid reference for that location. Use\n ghostty_grid_ref_cell() and ghostty_grid_ref_row() to extract the cell\n and row.\n\n Lookups using the `active` and `viewport` tags are fast. The `screen`\n and `history` tags may require traversing the full scrollback page list\n to resolve the y coordinate, so they can be expensive for large\n scrollback buffers.\n\n This function isn't meant to be used as the core of render loop. It\n isn't built to sustain the framerates needed for rendering large screens.\n Use the render state API for that. This API is instead meant for less\n strictly performance-sensitive use cases.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param point The point specifying which cell to look up\n @param[out] out_ref On success, set to the grid reference at the given point (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal\n         is NULL or the point is out of bounds\n\n @ingroup terminal"]
    pub fn ghostty_terminal_grid_ref(
        terminal: GhosttyTerminal,
        point: GhosttyPoint,
        out_ref: *mut GhosttyGridRef,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create an owned tracked grid reference for a terminal point.\n\n This is the tracked variant of ghostty_terminal_grid_ref(). The returned\n handle follows the referenced cell as the terminal's page list is modified:\n scrolling, pruning, resize/reflow, and other page-list operations update the\n tracked reference automatically.\n\n The reference is attached to the terminal screen/page-list that is active at\n creation time.\n\n If the point is outside the requested coordinate space, this returns\n GHOSTTY_INVALID_VALUE and writes NULL to out_ref.\n\n The returned handle must be freed with ghostty_tracked_grid_ref_free(). If\n the terminal is freed first, the handle remains valid only for\n tracked-grid-ref APIs: it reports no value and can still be freed.\n\n @param terminal Terminal instance.\n @param point Point to track.\n @param[out] out_ref On success, receives the tracked reference handle.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if terminal,\n         point, or out_ref is invalid, or GHOSTTY_OUT_OF_MEMORY if allocation\n         fails.\n\n @ingroup terminal"]
    pub fn ghostty_terminal_grid_ref_track(
        terminal: GhosttyTerminal,
        point: GhosttyPoint,
        out_ref: *mut GhosttyTrackedGridRef,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Convert a grid reference back to a point in the given coordinate system.\n\n This is the inverse of ghostty_terminal_grid_ref(): given a grid reference,\n it returns the x/y coordinates in the requested coordinate system (active,\n viewport, screen, or history).\n\n The grid reference must have been obtained from the same terminal instance.\n Like all grid references, it is only valid until the next mutating terminal\n call.\n\n Not every grid reference is representable in every coordinate system. For\n example, a cell in scrollback history cannot be expressed in active\n coordinates, and a cell that has scrolled off the visible area cannot be\n expressed in viewport coordinates. In these cases, the function returns\n GHOSTTY_NO_VALUE.\n\n @param terminal The terminal handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param ref Pointer to the grid reference to convert\n @param tag The target coordinate system\n @param[out] out On success, set to the coordinate in the requested system (may be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if the terminal\n         or ref is NULL/invalid, GHOSTTY_NO_VALUE if the ref falls outside\n         the requested coordinate system\n\n @ingroup terminal"]
    pub fn ghostty_terminal_point_from_grid_ref(
        terminal: GhosttyTerminal,
        ref_: *const GhosttyGridRef,
        tag: GhosttyPointTag,
        out: *mut GhosttyPointCoordinate,
    ) -> GhosttyResult;
}
#[doc = " Extra screen state to include in styled output.\n\n @ingroup formatter"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyFormatterScreenExtra {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyFormatterScreenExtra)."]
    pub size: usize,
    #[doc = " Emit cursor position using CUP (CSI H)."]
    pub cursor: bool,
    #[doc = " Emit current SGR style state based on the cursor's active style_id."]
    pub style: bool,
    #[doc = " Emit current hyperlink state using OSC 8 sequences."]
    pub hyperlink: bool,
    #[doc = " Emit character protection mode using DECSCA."]
    pub protection: bool,
    #[doc = " Emit Kitty keyboard protocol state using CSI > u and CSI = sequences."]
    pub kitty_keyboard: bool,
    #[doc = " Emit character set designations and invocations."]
    pub charsets: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyFormatterScreenExtra"]
        [::std::mem::size_of::<GhosttyFormatterScreenExtra>() - 16usize];
    ["Alignment of GhosttyFormatterScreenExtra"]
        [::std::mem::align_of::<GhosttyFormatterScreenExtra>() - 8usize];
    ["Offset of field: GhosttyFormatterScreenExtra::size"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, size) - 0usize];
    ["Offset of field: GhosttyFormatterScreenExtra::cursor"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, cursor) - 8usize];
    ["Offset of field: GhosttyFormatterScreenExtra::style"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, style) - 9usize];
    ["Offset of field: GhosttyFormatterScreenExtra::hyperlink"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, hyperlink) - 10usize];
    ["Offset of field: GhosttyFormatterScreenExtra::protection"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, protection) - 11usize];
    ["Offset of field: GhosttyFormatterScreenExtra::kitty_keyboard"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, kitty_keyboard) - 12usize];
    ["Offset of field: GhosttyFormatterScreenExtra::charsets"]
        [::std::mem::offset_of!(GhosttyFormatterScreenExtra, charsets) - 13usize];
};
#[doc = " Extra terminal state to include in styled output.\n\n @ingroup formatter"]
#[repr(C)]
#[derive(Debug, Default, Copy, Clone)]
pub struct GhosttyFormatterTerminalExtra {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyFormatterTerminalExtra)."]
    pub size: usize,
    #[doc = " Emit the palette using OSC 4 sequences."]
    pub palette: bool,
    #[doc = " Emit terminal modes that differ from their defaults using CSI h/l."]
    pub modes: bool,
    #[doc = " Emit scrolling region state using DECSTBM and DECSLRM sequences."]
    pub scrolling_region: bool,
    #[doc = " Emit tabstop positions by clearing all tabs and setting each one."]
    pub tabstops: bool,
    #[doc = " Emit the present working directory using OSC 7."]
    pub pwd: bool,
    #[doc = " Emit keyboard modes such as ModifyOtherKeys."]
    pub keyboard: bool,
    #[doc = " Screen-level extras."]
    pub screen: GhosttyFormatterScreenExtra,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyFormatterTerminalExtra"]
        [::std::mem::size_of::<GhosttyFormatterTerminalExtra>() - 32usize];
    ["Alignment of GhosttyFormatterTerminalExtra"]
        [::std::mem::align_of::<GhosttyFormatterTerminalExtra>() - 8usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::size"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, size) - 0usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::palette"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, palette) - 8usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::modes"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, modes) - 9usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::scrolling_region"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, scrolling_region) - 10usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::tabstops"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, tabstops) - 11usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::pwd"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, pwd) - 12usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::keyboard"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, keyboard) - 13usize];
    ["Offset of field: GhosttyFormatterTerminalExtra::screen"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalExtra, screen) - 16usize];
};
#[doc = " Options for creating a terminal formatter.\n\n @ingroup formatter"]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyFormatterTerminalOptions {
    #[doc = " Size of this struct in bytes. Must be set to sizeof(GhosttyFormatterTerminalOptions)."]
    pub size: usize,
    #[doc = " Output format to emit."]
    pub emit: GhosttyFormatterFormat,
    #[doc = " Whether to unwrap soft-wrapped lines."]
    pub unwrap: bool,
    #[doc = " Whether to trim trailing whitespace on non-blank lines."]
    pub trim: bool,
    #[doc = " Extra terminal state to include in styled output."]
    pub extra: GhosttyFormatterTerminalExtra,
    #[doc = " Optional selection to restrict output to a range.\n  If NULL, the entire screen is formatted."]
    pub selection: *const GhosttySelection,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyFormatterTerminalOptions"]
        [::std::mem::size_of::<GhosttyFormatterTerminalOptions>() - 56usize];
    ["Alignment of GhosttyFormatterTerminalOptions"]
        [::std::mem::align_of::<GhosttyFormatterTerminalOptions>() - 8usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::size"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, size) - 0usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::emit"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, emit) - 8usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::unwrap"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, unwrap) - 12usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::trim"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, trim) - 13usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::extra"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, extra) - 16usize];
    ["Offset of field: GhosttyFormatterTerminalOptions::selection"]
        [::std::mem::offset_of!(GhosttyFormatterTerminalOptions, selection) - 48usize];
};
