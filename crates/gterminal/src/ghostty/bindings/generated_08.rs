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
pub type GhosttyMouseEncoderOption = ::std::os::raw::c_int;
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
#[doc = " The user pasted from a clipboard: keybind, menu, middle click."]
pub const GhosttyPasteSource_GHOSTTY_PASTE_SOURCE_CLIPBOARD: GhosttyPasteSource = 0;
#[doc = " Text inserted some other way: IME commit, drag and drop, scripted\n input. Always written as text, never as a paste event, matching\n kitty. This is not a way to opt out of paste events; an embedder\n that doesn't want them doesn't install a clipboard_read callback."]
pub const GhosttyPasteSource_GHOSTTY_PASTE_SOURCE_TEXT: GhosttyPasteSource = 1;
#[doc = " Text inserted some other way: IME commit, drag and drop, scripted\n input. Always written as text, never as a paste event, matching\n kitty. This is not a way to opt out of paste events; an embedder\n that doesn't want them doesn't install a clipboard_read callback."]
pub const GhosttyPasteSource_GHOSTTY_PASTE_SOURCE_MAX_VALUE: GhosttyPasteSource = 2147483647;
#[doc = " Why a paste happened."]
pub type GhosttyPasteSource = ::std::os::raw::c_int;
#[doc = " A paste of clipboard contents into the terminal.\n\n This is a sized struct; set `size` to `sizeof(GhosttyPaste)`. The\n MIME type array and the strings it points to are borrowed only for\n the duration of the ghostty_terminal_paste() call, as is everything\n the reader produces."]
#[repr(C)]
#[derive(Debug, Copy, Clone)]
pub struct GhosttyPaste {
    #[doc = " Size of this struct in bytes."]
    pub size: usize,
    #[doc = " The clipboard the contents came from. Reported to the program on a\n paste event (the selection and primary locations are both reported\n as the primary selection, the protocol knows only two); no effect\n on a text paste."]
    pub location: GhosttyClipboardLocation,
    #[doc = " Why this paste happened."]
    pub source: GhosttyPasteSource,
    #[doc = " Borrowed array of the MIME types of the representations available,\n in preferred order. A text paste reads and writes the first entry\n with a text MIME type such as \"text/plain\" and ignores the rest. A\n paste event lists every entry and reads none. May be NULL when\n mimes_len is zero, which is nothing to paste."]
    pub mimes: *const GhosttyString,
    #[doc = " Number of entries in mimes."]
    pub mimes_len: usize,
    #[doc = " Produces the data of a representation on demand. Required when\n mimes_len is nonzero.\n\n Called at most once per ghostty_terminal_paste() call: for the\n text representation being pasted, never for anything else and\n never for a paste event. The MIME type requested is always an\n entry of `mimes`, passed through exactly as given there (the same\n pointer and length), so the callback may identify the\n representation by pointer or by content. A false return fails the\n paste with GHOSTTY_IO_ERROR."]
    pub reader: GhosttyMimeReader,
    #[doc = " Write text that could inject commands. Call with false, confirm\n with the user on GHOSTTY_REJECTED, and call again with true."]
    pub allow_unsafe: bool,
}
#[allow(clippy::unnecessary_operation, clippy::identity_op)]
const _: () = {
    ["Size of GhosttyPaste"][::std::mem::size_of::<GhosttyPaste>() - 56usize];
    ["Alignment of GhosttyPaste"][::std::mem::align_of::<GhosttyPaste>() - 8usize];
    ["Offset of field: GhosttyPaste::size"][::std::mem::offset_of!(GhosttyPaste, size) - 0usize];
    ["Offset of field: GhosttyPaste::location"]
        [::std::mem::offset_of!(GhosttyPaste, location) - 8usize];
    ["Offset of field: GhosttyPaste::source"]
        [::std::mem::offset_of!(GhosttyPaste, source) - 12usize];
    ["Offset of field: GhosttyPaste::mimes"][::std::mem::offset_of!(GhosttyPaste, mimes) - 16usize];
    ["Offset of field: GhosttyPaste::mimes_len"]
        [::std::mem::offset_of!(GhosttyPaste, mimes_len) - 24usize];
    ["Offset of field: GhosttyPaste::reader"]
        [::std::mem::offset_of!(GhosttyPaste, reader) - 32usize];
    ["Offset of field: GhosttyPaste::allow_unsafe"]
        [::std::mem::offset_of!(GhosttyPaste, allow_unsafe) - 48usize];
};
impl Default for GhosttyPaste {
    fn default() -> Self {
        let mut s = ::std::mem::MaybeUninit::<Self>::uninit();
        unsafe {
            ::std::ptr::write_bytes(s.as_mut_ptr(), 0, 1);
            s.assume_init()
        }
    }
}
unsafe extern "C" {
    #[doc = " Paste into the terminal according to its current state: a Kitty\n clipboard protocol paste event if mode 5522 is enabled and a\n clipboard_read callback is installed, otherwise the text framed per\n mode 2004. See the group documentation for the full behavior. Output\n streams through the write_pty callback in chunks. The viewport is not\n scrolled; that is up to the embedder, as for key input.\n\n A paste event records a session grant for its one-time password only\n once the event is written; a failed call never leaves a grant for an\n event that was never sent.\n\n @param terminal The terminal handle\n @param paste The paste request, borrowed for the duration of the call\n @param[out] out_written On success, whether anything was written to\n             the pty (the encoded text or a paste event). False means\n             there was nothing to paste: no non-empty text\n             representation. May be NULL.\n @return GHOSTTY_SUCCESS on success (see @p out_written);\n         GHOSTTY_REJECTED if the text could inject commands and\n         GhosttyPaste::allow_unsafe is false (nothing was written);\n         GHOSTTY_INVALID_VALUE for a NULL terminal or paste, MIME\n         types without a reader, or when no write_pty callback is\n         installed; GHOSTTY_OUT_OF_MEMORY; GHOSTTY_IO_ERROR if the\n         reader failed or there is no secure entropy source to mint a\n         paste event password (wasm32-freestanding without\n         GHOSTTY_SYS_OPT_RANDOM_SECURE set). Errors write nothing."]
    pub fn ghostty_terminal_paste(
        terminal: GhosttyTerminal,
        paste: *const GhosttyPaste,
        out_written: *mut bool,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Check if paste data is safe to paste into the terminal.\n\n Data is considered unsafe if it contains:\n - Newlines (`\\n`) which can inject commands\n - The bracketed paste end sequence (`\\x1b[201~`) which can be used\n   to exit bracketed paste mode and inject commands\n\n This check is conservative and considers data unsafe regardless of\n current terminal state. ghostty_terminal_paste() applies the\n terminal-state-aware rule itself (newlines are safe inside a\n bracketed paste); use this to apply the stricter rule on top.\n\n @param data The paste data to check (must not be NULL)\n @param len The length of the data in bytes\n @return true if the data is safe to paste, false otherwise"]
    pub fn ghostty_paste_is_safe(data: *const ::std::os::raw::c_char, len: usize) -> bool;
}
unsafe extern "C" {
    #[doc = " Encode paste data for writing to the terminal pty.\n\n This function prepares paste data for terminal input by:\n - Stripping unsafe control bytes (NUL, ESC, DEL, etc.) by replacing\n   them with spaces\n - Wrapping the data in bracketed paste sequences if @p bracketed is true\n - Replacing newlines with carriage returns if @p bracketed is false\n\n The input @p data buffer is modified in place during encoding. The\n encoded result (potentially with bracketed paste prefix/suffix) is\n written to the output buffer.\n\n If the output buffer is too small, the function returns\n GHOSTTY_OUT_OF_SPACE and sets the required size in @p out_written.\n The caller can then retry with a sufficiently sized buffer.\n\n This is the encoder ghostty_terminal_paste() uses for a text paste;\n use it directly when there is no terminal to paste into.\n\n @param data The paste data to encode (modified in place, may be NULL)\n @param data_len The length of the input data in bytes\n @param bracketed Whether bracketed paste mode is active\n @param buf Output buffer to write the encoded result into (may be NULL)\n @param buf_len Size of the output buffer in bytes\n @param[out] out_written On success, the number of bytes written. On\n             GHOSTTY_OUT_OF_SPACE, the required buffer size.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_OUT_OF_SPACE if the buffer\n         is too small"]
    pub fn ghostty_paste_encode(
        data: *mut ::std::os::raw::c_char,
        data_len: usize,
        bracketed: bool,
        buf: *mut ::std::os::raw::c_char,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " ghostty_search_tick() can make progress without terminal access."]
pub const GhosttySearchStatus_GHOSTTY_SEARCH_STATUS_RUNNING: GhosttySearchStatus = 0;
#[doc = " Blocked until ghostty_search_feed(). This is also the state right\n after a needle is set, since the search has not yet seen the\n terminal."]
pub const GhosttySearchStatus_GHOSTTY_SEARCH_STATUS_FEED_REQUIRED: GhosttySearchStatus = 1;
#[doc = " Caught up with the terminal state as of the last feed. This never\n means finished forever, since later terminal writes require\n another feed to be seen. A search with no needle set also reports\n complete, since there is nothing to look for."]
pub const GhosttySearchStatus_GHOSTTY_SEARCH_STATUS_COMPLETE: GhosttySearchStatus = 2;
#[doc = " Caught up with the terminal state as of the last feed. This never\n means finished forever, since later terminal writes require\n another feed to be seen. A search with no needle set also reports\n complete, since there is nothing to look for."]
pub const GhosttySearchStatus_GHOSTTY_SEARCH_STATUS_MAX_VALUE: GhosttySearchStatus = 2147483647;
#[doc = " Progress state of a search.\n\n @ingroup search"]
pub type GhosttySearchStatus = ::std::os::raw::c_int;
#[doc = " Scroll the viewport so the match is visible, only if it is not\n already visible. This is the default."]
pub const GhosttySearchScroll_GHOSTTY_SEARCH_SCROLL_IF_NEEDED: GhosttySearchScroll = 0;
#[doc = " Never scroll the viewport."]
pub const GhosttySearchScroll_GHOSTTY_SEARCH_SCROLL_NONE: GhosttySearchScroll = 1;
#[doc = " Never scroll the viewport."]
pub const GhosttySearchScroll_GHOSTTY_SEARCH_SCROLL_MAX_VALUE: GhosttySearchScroll = 2147483647;
#[doc = " Scroll policy applied when a match becomes selected via\n GHOSTTY_SEARCH_OPT_SELECT_NEXT or GHOSTTY_SEARCH_OPT_SELECT_PREV.\n\n @ingroup search"]
pub type GhosttySearchScroll = ::std::os::raw::c_int;
#[doc = " Current search status: GhosttySearchStatus*."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_STATUS: GhosttySearchData = 0;
#[doc = " The needle this search is looking for: GhosttyString*. The bytes\n are borrowed from the search and remain valid until the needle is\n changed or the search is freed. Returns GHOSTTY_NO_VALUE when no\n needle is set."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_NEEDLE: GhosttySearchData = 1;
#[doc = " Total matches found so far on the active screen: size_t*. Zero\n until the first feed."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_TOTAL_MATCHES: GhosttySearchData = 2;
#[doc = " Index of the selected match: size_t*. This indexes the newest to\n oldest ordering of GHOSTTY_SEARCH_DATA_MATCHES, where 0 is the\n newest match, so a \"k of n\" find bar renders index + 1 of\n GHOSTTY_SEARCH_DATA_TOTAL_MATCHES. Returns GHOSTTY_NO_VALUE when\n nothing is selected."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_SELECTED_INDEX: GhosttySearchData = 3;
#[doc = " The selected match: GhosttySelection*. This is an untracked\n snapshot with standard GhosttySelection lifetime rules. Returns\n GHOSTTY_NO_VALUE when nothing is selected."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_SELECTED_MATCH: GhosttySearchData = 4;
#[doc = " All matches on the active screen, ordered newest to oldest, from\n the bottom of the active area up through scrollback:\n GhosttySelectionBuffer*. Set ptr to NULL with cap 0 to query the\n required capacity. An undersized buffer returns\n GHOSTTY_OUT_OF_SPACE with the required capacity in len."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_MATCHES: GhosttySearchData = 5;
#[doc = " Matches on the pages covering the viewport, for drawing highlight\n rectangles: GhosttySelectionBuffer*. The list is computed during\n feeds and cached, so it reflects the viewport as of the last\n feed.\n\n Matches are found a page at a time, so the list can include\n matches slightly outside the visible viewport when they share a\n page with it. Ghostty's own renderer behaves the same way.\n Converting each match to viewport coordinates with\n ghostty_terminal_point_from_grid_ref() clips this naturally: skip\n matches that fail the conversion or whose row is beyond the\n visible row count."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_VIEWPORT_MATCHES: GhosttySearchData = 6;
#[doc = " Current scroll policy: GhosttySearchScroll*."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_SELECT_SCROLL: GhosttySearchData = 7;
#[doc = " Current scroll policy: GhosttySearchScroll*."]
pub const GhosttySearchData_GHOSTTY_SEARCH_DATA_MAX_VALUE: GhosttySearchData = 2147483647;
#[doc = " Data fields readable with ghostty_search_get(). The output value\n type is documented per field.\n\n All reads reflect the terminal's active screen as of the last feed.\n When the running application switches to the alternate screen, the\n next feed switches counts, matches, and selection to that screen's\n results. Primary screen results, including completed scrollback\n searches, are retained and restored on the way back.\n\n @ingroup search"]
pub type GhosttySearchData = ::std::os::raw::c_int;
#[doc = " Set the needle to search for: const GhosttyString*. The bytes are\n copied, so the caller's memory does not need to outlive the call.\n Matching is byte-exact except ASCII letters, which compare\n case-insensitively.\n\n Changing the needle restarts the search from scratch and drops\n all results. As an exception, setting a needle equal to the current\n one (compared the same way as matching) keeps existing results,\n so find bars can resubmit freely. A NULL or empty value clears\n the needle and returns the search to idle.\n\n Replacing or clearing a needle releases tracked state held\n within the terminal, so the caller must serialize this with all\n other access to the same terminal. Returns GHOSTTY_INVALID_VALUE\n after the terminal was freed."]
pub const GhosttySearchOption_GHOSTTY_SEARCH_OPT_NEEDLE: GhosttySearchOption = 0;
#[doc = " Select the next match, moving toward older content: from the\n bottom of the screen upward into history, the direction a search\n from the prompt usually wants. Wraps around past the oldest\n match.\n\n The value must be NULL. It is reserved for future use.\n\n This catches up with the terminal first, so it is safe to call at\n any time relative to feeds. The viewport scrolls to the newly\n selected match according to GHOSTTY_SEARCH_OPT_SELECT_SCROLL.\n This reads the terminal, so the caller must serialize it with all\n other access to the same terminal. Returns GHOSTTY_NO_VALUE when\n there are no matches."]
pub const GhosttySearchOption_GHOSTTY_SEARCH_OPT_SELECT_NEXT: GhosttySearchOption = 1;
#[doc = " Select the previous match, moving toward newer content, wrapping\n around past the newest match. Otherwise identical to\n GHOSTTY_SEARCH_OPT_SELECT_NEXT."]
pub const GhosttySearchOption_GHOSTTY_SEARCH_OPT_SELECT_PREV: GhosttySearchOption = 2;
#[doc = " Set the scroll policy applied by the select options: const\n GhosttySearchScroll*. The policy persists until changed. A NULL\n value resets it to GHOSTTY_SEARCH_SCROLL_IF_NEEDED. This only\n modifies search-owned state and never reads the terminal."]
pub const GhosttySearchOption_GHOSTTY_SEARCH_OPT_SELECT_SCROLL: GhosttySearchOption = 3;
#[doc = " Set the scroll policy applied by the select options: const\n GhosttySearchScroll*. The policy persists until changed. A NULL\n value resets it to GHOSTTY_SEARCH_SCROLL_IF_NEEDED. This only\n modifies search-owned state and never reads the terminal."]
pub const GhosttySearchOption_GHOSTTY_SEARCH_OPT_MAX_VALUE: GhosttySearchOption = 2147483647;
#[doc = " Options writable with ghostty_search_set(). The value type, and\n what a NULL value means, is documented per option.\n\n @ingroup search"]
pub type GhosttySearchOption = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Create a search bound to a terminal.\n\n The search borrows the terminal and never frees it. The search and\n the terminal can be freed in either order; see ghostty_search_free().\n\n The search starts idle with no needle: it reports\n GHOSTTY_SEARCH_STATUS_COMPLETE and finds nothing. Set\n GHOSTTY_SEARCH_OPT_NEEDLE to start searching.\n\n Creation is cheap and does not read terminal contents, but it\n registers the search with the terminal so the two can be freed in\n any order. The caller must serialize this call with all other\n access to the same terminal.\n\n @param allocator Allocator, or NULL for the default allocator\n @param out_search Receives the created search handle\n @param terminal Terminal to bind the search to\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if\n         out_search or terminal is invalid, or GHOSTTY_OUT_OF_MEMORY\n         if allocation fails\n\n @ingroup search"]
    pub fn ghostty_search_new(
        allocator: *const GhosttyAllocator,
        out_search: *mut GhosttySearch,
        terminal: GhosttyTerminal,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a search.\n\n If the bound terminal is still alive, this releases tracked state\n the search holds within it, so the caller must serialize this call\n with all other access to the same terminal. If the terminal was\n already freed, the search has been detached and this releases only\n search-owned memory. Passing NULL is allowed and is a no-op.\n\n @param search Search handle to free\n\n @ingroup search"]
    pub fn ghostty_search_free(search: GhosttySearch);
}
unsafe extern "C" {
    #[doc = " Make a bounded amount of search progress.\n\n This only works on data the search has already copied and never\n reads the terminal, so it is safe to call while another thread\n modifies the terminal. Call it in a loop while the status is\n GHOSTTY_SEARCH_STATUS_RUNNING. When the status becomes\n GHOSTTY_SEARCH_STATUS_FEED_REQUIRED, call ghostty_search_feed() to\n unblock it.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param[out] out_status Receives the status after the tick (may be NULL)\n @return GHOSTTY_SUCCESS on success, or GHOSTTY_INVALID_VALUE if\n         search is NULL\n\n @ingroup search"]
    pub fn ghostty_search_tick(
        search: GhosttySearch,
        out_status: *mut GhosttySearchStatus,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Read the terminal to update the search.\n\n Each feed catches the search up with the terminal: it reconciles\n the tracked screens against the live ones, re-scans the active\n area, refreshes the viewport match list, gives the scrollback\n searcher its next chunk of data, and prunes results that scrollback\n eviction invalidated. Feeding is also the only way the search\n learns about terminal changes, so keep feeding periodically while\n the search is in use, even after it reports complete.\n\n This reads the terminal, so the caller must serialize it with all\n other access to the same terminal. Each call does a bounded amount\n of work so that any caller-held terminal lock is held only briefly.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, or GHOSTTY_INVALID_VALUE if\n         search is NULL or the terminal was freed\n\n @ingroup search"]
    pub fn ghostty_search_feed(search: GhosttySearch) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Feed and tick until the search is caught up with the terminal.\n\n This is a blocking convenience for one-shot and single-threaded\n embedders. It always performs at least one feed, so it also picks\n up any terminal changes since the last feed, then loops until the\n status is GHOSTTY_SEARCH_STATUS_COMPLETE. Searching a large\n scrollback can take a while, so interactive embedders should drive\n ghostty_search_tick() and ghostty_search_feed() themselves.\n\n This reads the terminal for the entire call, so the caller must\n serialize it with all other access to the same terminal.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @return GHOSTTY_SUCCESS on success, or GHOSTTY_INVALID_VALUE if\n         search is NULL or the terminal was freed\n\n @ingroup search"]
    pub fn ghostty_search_run(search: GhosttySearch) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Write an option to a search.\n\n The value type, and what a NULL value means, depends on the option\n and is documented by GhosttySearchOption. The needle and select\n options touch the terminal, so the caller must serialize those\n calls with all other access to the same terminal.\n GHOSTTY_SEARCH_OPT_SELECT_SCROLL only modifies search-owned state.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param option Option to write\n @param value Pointer to the input value for the option. The meaning\n              of NULL is documented per option.\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if a select\n         option found no matches, GHOSTTY_OUT_OF_MEMORY if\n         allocation fails, or GHOSTTY_INVALID_VALUE if search,\n         option, or value is invalid or the option needs a terminal\n         that was already freed\n\n @ingroup search"]
    pub fn ghostty_search_set(
        search: GhosttySearch,
        option: GhosttySearchOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Read a data field from a search.\n\n The output value type depends on data and is documented by\n GhosttySearchData. This never reads the terminal, so it is safe to\n call while another thread modifies the terminal. Returned\n selections are untracked snapshots with standard GhosttySelection\n lifetime rules.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param data Data field to read\n @param value Output pointer whose type depends on data\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the\n         requested data has no value, GHOSTTY_OUT_OF_SPACE if a\n         provided GhosttySelectionBuffer is too small (required\n         capacity in its len), GHOSTTY_OUT_OF_MEMORY if collecting\n         viewport matches fails, or GHOSTTY_INVALID_VALUE if search,\n         data, or value is invalid\n\n @ingroup search"]
    pub fn ghostty_search_get(
        search: GhosttySearch,
        data: GhosttySearchData,
        value: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Read multiple data fields from a search in a single call.\n\n This is an optimization over calling ghostty_search_get() multiple\n times. Each entry in values must point to storage of the type\n documented by the corresponding GhosttySearchData key.\n\n If any individual read fails, the function returns that error and\n writes the index of the failing key to out_written when out_written\n is non-NULL. Earlier keys have already been written. On success,\n out_written receives count when non-NULL. A too-small\n GhosttySelectionBuffer stops the batch with GHOSTTY_OUT_OF_SPACE at\n that key's index with the required capacity in its len, so order\n buffer-valued keys after scalar keys.\n\n @param search Search handle (NULL returns GHOSTTY_INVALID_VALUE)\n @param count Number of data fields to read\n @param keys Data fields to read (must not be NULL)\n @param values Output pointers corresponding to keys (must not be NULL)\n @param out_written Optional number of fields read, or failing index\n                    on error\n @return GHOSTTY_SUCCESS on success, or the first failing read's\n         result\n\n @ingroup search"]
    pub fn ghostty_search_get_multi(
        search: GhosttySearch,
        count: usize,
        keys: *const GhosttySearchData,
        values: *mut *mut ::std::os::raw::c_void,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
#[doc = " Largest non-ground continuation the decoder will accept.\n\n A value of zero accepts only snapshots whose VT parser is in the ground\n state. The decoder default matches the largest built-in APC protocol\n buffer limit, currently 65 MiB.\n\n This is primarily an input validation limit. When\n GHOSTTY_SNAPSHOT_DECODER_OPT_RETAIN_CONTINUATION is true, the same value\n also becomes the continuation tracking limit on the returned terminal.\n\n Input type: size_t *"]
pub const GhosttySnapshotDecoderOption_GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_CONTINUATION_BYTES:
    GhosttySnapshotDecoderOption = 0;
#[doc = " Retain the decoded continuation on the returned terminal.\n\n When true, terminals returned by ghostty_snapshot_decoder_ready() and\n ghostty_snapshot_decoder_decode() use\n GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_CONTINUATION_BYTES as their continuation\n tracking limit. The existing ghostty_terminal_continuation_* APIs can then\n export the exact unfinished VT or UTF-8 input restored from the snapshot.\n\n This is false by default. A maximum continuation size of zero leaves\n tracking disabled. With a nonzero maximum, tracking remains enabled even\n when the decoded continuation is empty. Exporting an empty continuation\n does not disable it. Callers that do not need ongoing tracking must still\n set GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to zero after export and\n before writing post-snapshot input.\n\n Input type: bool *"]
pub const GhosttySnapshotDecoderOption_GHOSTTY_SNAPSHOT_DECODER_OPT_RETAIN_CONTINUATION:
    GhosttySnapshotDecoderOption = 1;
#[doc = " Retain the decoded continuation on the returned terminal.\n\n When true, terminals returned by ghostty_snapshot_decoder_ready() and\n ghostty_snapshot_decoder_decode() use\n GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_CONTINUATION_BYTES as their continuation\n tracking limit. The existing ghostty_terminal_continuation_* APIs can then\n export the exact unfinished VT or UTF-8 input restored from the snapshot.\n\n This is false by default. A maximum continuation size of zero leaves\n tracking disabled. With a nonzero maximum, tracking remains enabled even\n when the decoded continuation is empty. Exporting an empty continuation\n does not disable it. Callers that do not need ongoing tracking must still\n set GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to zero after export and\n before writing post-snapshot input.\n\n Input type: bool *"]
pub const GhosttySnapshotDecoderOption_GHOSTTY_SNAPSHOT_DECODER_OPT_MAX_VALUE:
    GhosttySnapshotDecoderOption = 2147483647;
#[doc = " Configurable snapshot decoder options.\n\n Options may only be changed before decoding starts. Calling\n ghostty_snapshot_decoder_set() after ghostty_snapshot_decoder_ready() or\n ghostty_snapshot_decoder_decode() returns GHOSTTY_INVALID_VALUE."]
pub type GhosttySnapshotDecoderOption = ::std::os::raw::c_int;
#[doc = " Invalid data type. Never results in data extraction."]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_INVALID:
    GhosttySnapshotDecoderData = 0;
#[doc = " Current maximum accepted continuation size.\n\n This value is available in every non-failed decoder state.\n\n Output type: size_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_MAX_CONTINUATION_BYTES:
    GhosttySnapshotDecoderData = 1;
#[doc = " Number of snapshot source bytes consumed so far.\n\n At FINISH this identifies the first byte after the snapshot. Trailing\n bytes are not consumed. This value is unavailable after a decoding error,\n because the decoder can no longer guarantee its source position.\n\n Output type: size_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_SOURCE_OFFSET:
    GhosttySnapshotDecoderData = 2;
#[doc = " Advisory complete logical history extent for the primary screen.\n\n The value counts rows before the active area, including any resident\n overlap carried before READY. It becomes available after READY validates.\n\n Output type: uint64_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_HISTORY_ROWS_PRIMARY:
    GhosttySnapshotDecoderData = 3;
#[doc = " Advisory complete logical history extent for the alternate screen.\n\n The value has the same semantics and lifetime as\n GHOSTTY_SNAPSHOT_DECODER_DATA_HISTORY_ROWS_PRIMARY. Querying it returns\n GHOSTTY_NO_VALUE when the snapshot does not declare an alternate screen.\n\n Output type: uint64_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_HISTORY_ROWS_ALTERNATE:
    GhosttySnapshotDecoderData = 4;
#[doc = " Screen associated with the most recently decoded history page.\n\n This value is available only after ghostty_snapshot_decoder_next()\n returns GHOSTTY_SUCCESS. A later call to next replaces it or clears it\n when FINISH is reached or an error occurs.\n\n Output type: GhosttyTerminalScreen *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_PROGRESS_SCREEN:
    GhosttySnapshotDecoderData = 5;
#[doc = " Rows prepended by the most recently decoded history page.\n\n Zero means the page was consumed and validated but could not be\n applied to the live terminal.\n\n Output type: size_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_PROGRESS_ROWS:
    GhosttySnapshotDecoderData = 6;
#[doc = " Page records remaining in the same screen's HISTORY sequence.\n\n This is not a count of all pages remaining in the snapshot.\n\n Output type: uint32_t *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_PROGRESS_REMAINING:
    GhosttySnapshotDecoderData = 7;
#[doc = " Whether decoded continuation tracking is retained on returned terminals.\n\n This value is available in every non-failed decoder state.\n\n Output type: bool *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_RETAIN_CONTINUATION:
    GhosttySnapshotDecoderData = 8;
#[doc = " Whether decoded continuation tracking is retained on returned terminals.\n\n This value is available in every non-failed decoder state.\n\n Output type: bool *"]
pub const GhosttySnapshotDecoderData_GHOSTTY_SNAPSHOT_DECODER_DATA_MAX_VALUE:
    GhosttySnapshotDecoderData = 2147483647;
#[doc = " Queryable snapshot decoder data.\n\n Each variant documents the output pointer type expected by\n ghostty_snapshot_decoder_get()."]
pub type GhosttySnapshotDecoderData = ::std::os::raw::c_int;
unsafe extern "C" {
    #[doc = " Encode a complete terminal snapshot to a writer.\n\n The terminal's persistent VT stream supplies the continuation bytes needed\n to reconstruct unfinished parser state. The caller must prevent concurrent\n writes or other terminal mutation for the duration of this call. The writer\n callback must not call terminal APIs with the same terminal handle.\n A terminal can be encoded with tracking disabled when its VT parser and\n UTF-8 decoder are both at ground. If either is unfinished, tracking must\n have been enabled before the input that produced that state was written;\n otherwise this returns GHOSTTY_INVALID_VALUE.\n\n Encoding begins at the writer's current position. If an error occurs, the\n writer may contain a partial snapshot without a valid FINISH marker.\n Calls to the writer are synchronous; this function does not flush or make\n the caller's destination durable.\n\n @param terminal Terminal to encode (must not be NULL)\n @param writer Destination writer whose write callback must not be NULL\n @return GHOSTTY_SUCCESS on success, GHOSTTY_IO_ERROR if the writer rejects\n         output, GHOSTTY_LIMIT_EXCEEDED if output accounting overflows, or\n         another error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_encode(
        terminal: GhosttyTerminal,
        writer: GhosttyWriter,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Encode a complete terminal snapshot to a caller-provided buffer.\n\n Pass NULL for buf with buf_len zero to query the required size. If the\n buffer is too small, this returns GHOSTTY_OUT_OF_SPACE and stores the\n required capacity in out_written. A non-NULL undersized buffer may contain\n a partial snapshot prefix. On success, out_written receives the number of\n bytes encoded.\n\n A terminal can be encoded with tracking disabled when its VT parser and\n UTF-8 decoder are both at ground. If either is unfinished, tracking must\n have been enabled before the input that produced that state was written;\n otherwise this returns GHOSTTY_INVALID_VALUE.\n\n @param terminal Terminal to encode (must not be NULL)\n @param buf Destination buffer, or NULL when buf_len is zero\n @param buf_len Destination buffer capacity in bytes\n @param[out] out_written Bytes written, or required capacity on\n             GHOSTTY_OUT_OF_SPACE (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_encode_buf(
        terminal: GhosttyTerminal,
        buf: *mut u8,
        buf_len: usize,
        out_written: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Encode a complete terminal snapshot to an allocated buffer.\n\n The returned buffer is allocated with allocator, or the default allocator\n when allocator is NULL. The caller must release it with ghostty_free(),\n passing the same allocator used here.\n\n A terminal can be encoded with tracking disabled when its VT parser and\n UTF-8 decoder are both at ground. If either is unfinished, tracking must\n have been enabled before the input that produced that state was written;\n otherwise this returns GHOSTTY_INVALID_VALUE.\n\n @param terminal Terminal to encode (must not be NULL)\n @param allocator Allocator for the output, or NULL for the default allocator\n @param[out] out_ptr Allocated snapshot bytes (must not be NULL)\n @param[out] out_len Number of allocated snapshot bytes (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_encode_alloc(
        terminal: GhosttyTerminal,
        allocator: *const GhosttyAllocator,
        out_ptr: *mut *mut u8,
        out_len: *mut usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a snapshot decoder that reads from a caller-provided reader.\n\n The decoder stores a copy of reader. Its read callback must not be NULL, and\n both the callback and its caller-owned context must remain valid until\n FINISH is reached or the decoder is freed. Reads are synchronous and occur\n only during ready, next, or decode calls. A zero-byte successful read is\n permanent end-of-file, not temporary starvation; nonblocking sources must\n wait outside the decoder or block in their callback. The read callback must\n not call APIs, including ghostty_snapshot_decoder_free(), on the decoder\n that owns it. Returning false reports GHOSTTY_IO_ERROR; returning true with\n zero bytes before a required marker reports truncated snapshot data as\n GHOSTTY_INVALID_VALUE.\n\n @param allocator Allocator for decoder and decoded terminal state, or NULL\n                  for the default allocator\n @param decoder Pointer to receive the decoder handle (must not be NULL)\n @param reader Snapshot source reader\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_new(
        allocator: *const GhosttyAllocator,
        decoder: *mut GhosttySnapshotDecoder,
        reader: GhosttyReader,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Create a snapshot decoder over a borrowed byte buffer.\n\n The bytes are not copied. ptr must remain valid and immutable until FINISH\n is reached or the decoder is freed. Bytes after FINISH are not consumed;\n query GHOSTTY_SNAPSHOT_DECODER_DATA_SOURCE_OFFSET to locate them.\n\n @param allocator Allocator for decoder and decoded terminal state, or NULL\n                  for the default allocator\n @param decoder Pointer to receive the decoder handle (must not be NULL)\n @param ptr Snapshot source bytes\n @param len Number of source bytes\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_new_buf(
        allocator: *const GhosttyAllocator,
        decoder: *mut GhosttySnapshotDecoder,
        ptr: *const u8,
        len: usize,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Free a snapshot decoder.\n\n This does not release the caller's ownership of a terminal returned by\n ready or decode. Abandoning an incremental decode leaves that terminal\n usable with whatever history had already been restored.\n\n @param decoder Decoder to free (may be NULL)\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_free(decoder: GhosttySnapshotDecoder);
}
unsafe extern "C" {
    #[doc = " Set a snapshot decoder option.\n\n The value pointer must have the type documented by option. Options may only\n be changed before decoding starts.\n\n @param decoder Decoder handle (must not be NULL)\n @param option Option to change\n @param value Pointer to the option value (must not be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_INVALID_VALUE if decoding has\n         started or an argument is invalid, or another error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_set(
        decoder: GhosttySnapshotDecoder,
        option: GhosttySnapshotDecoderOption,
        value: *const ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Decode and validate the renderable snapshot prefix through READY.\n\n On success, terminal receives a caller-owned terminal with its persistent\n VT stream already restored from the snapshot continuation. The terminal is\n immediately usable for rendering and live input. Older scrollback remains\n to be restored with ghostty_snapshot_decoder_next().\n\n The restored parser state may be unfinished. By default, terminal\n continuation tracking is disabled and\n GHOSTTY_TERMINAL_DATA_CONTINUATION_MAX_BYTES returns zero. When\n GHOSTTY_SNAPSHOT_DECODER_OPT_RETAIN_CONTINUATION is true, the decoder's\n maximum continuation size is applied to the terminal, and the terminal\n continuation APIs export the exact current continuation when that limit is\n nonzero. Tracking remains enabled even if the exported continuation is\n empty. Callers that do not need ongoing tracking must set\n GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to zero after export and before\n writing any post-snapshot bytes, because later input may change it.\n\n The caller must keep the returned terminal alive until FINISH validates or\n the decoder is freed. The decoder borrows this terminal handle while it\n restores history; ghostty_snapshot_decoder_next() uses it automatically.\n\n This operation may only be called once and only before decoding starts.\n terminal is set to NULL on every error. A decoding, I/O, or allocation\n error after input consumption begins poisons the decoder, after which it\n must be freed. An invalid argument or lifecycle error detected before the\n operation consumes input does not poison it.\n\n @param decoder Decoder handle (must not be NULL)\n @param[out] terminal Pointer to receive the terminal (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_ready(
        decoder: GhosttySnapshotDecoder,
        terminal: *mut GhosttyTerminal,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Decode one history page into the terminal returned by READY.\n\n Each GHOSTTY_SUCCESS consumes and validates one PAGE record. Query the\n GHOSTTY_SNAPSHOT_DECODER_DATA_PROGRESS_* values before calling next again.\n GHOSTTY_NO_VALUE means FINISH was validated; repeated calls after FINISH\n also return GHOSTTY_NO_VALUE.\n\n The terminal may be rendered, resized, and fed live PTY input between calls.\n If a history page can no longer be applied safely, it is still consumed and\n validated and progress reports zero rows. The decoder applies history\n to the caller-owned terminal produced by its READY operation.\n\n A decoding error invalidates the decoder's source position. The terminal\n remains caller-owned and usable with its already-restored history, but only\n ghostty_snapshot_decoder_free() may subsequently be called on the decoder.\n\n @param decoder Decoder handle (must not be NULL)\n @return GHOSTTY_SUCCESS for one page, GHOSTTY_NO_VALUE after FINISH, or an\n         error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_next(decoder: GhosttySnapshotDecoder) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Decode and validate one complete snapshot.\n\n This is the one-shot form of READY followed by all history pages through\n FINISH. It may only be called before decoding starts. Bytes following FINISH\n are left unread. On success terminal receives a caller-owned terminal with\n its persistent VT stream restored. Continuation tracking on the returned\n terminal is disabled by default. When\n GHOSTTY_SNAPSHOT_DECODER_OPT_RETAIN_CONTINUATION is true, the decoder's\n maximum continuation size is applied to the terminal, and the terminal\n continuation APIs export the exact current continuation when that limit is\n nonzero. Tracking remains enabled even if the exported continuation is\n empty. Callers that do not need ongoing tracking must set\n GHOSTTY_TERMINAL_OPT_CONTINUATION_MAX_BYTES to zero after export and before\n writing any post-snapshot bytes, because later input may change it.\n terminal is set to NULL on every error.\n A decoding, I/O, or allocation error after input consumption begins poisons\n the decoder, after which it must be freed. An invalid argument or\n lifecycle error detected before the operation consumes input does not\n poison it.\n\n @param decoder Decoder handle (must not be NULL)\n @param[out] terminal Pointer to receive the terminal (must not be NULL)\n @return GHOSTTY_SUCCESS on success, or an error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_decode(
        decoder: GhosttySnapshotDecoder,
        terminal: *mut GhosttyTerminal,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get typed data from a snapshot decoder.\n\n The output pointer must have the type documented by data. A phase-dependent\n value that is not currently available returns GHOSTTY_NO_VALUE.\n\n @param decoder Decoder handle (must not be NULL)\n @param data Data kind to query\n @param[out] out Pointer to receive the value (must not be NULL)\n @return GHOSTTY_SUCCESS on success, GHOSTTY_NO_VALUE if the requested data\n         is unavailable, or another error code on failure\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_get(
        decoder: GhosttySnapshotDecoder,
        data: GhosttySnapshotDecoderData,
        out: *mut ::std::os::raw::c_void,
    ) -> GhosttyResult;
}
unsafe extern "C" {
    #[doc = " Get multiple snapshot decoder data fields in a single call.\n\n Each keys element selects a data kind and the corresponding values element\n points to storage of the documented output type. Processing stops at the\n first error. On success out_written is set to count; on error it is set to\n the number of values written before the failing key. Invalid array arguments\n report zero values written.\n\n @param decoder Decoder handle (must not be NULL)\n @param count Number of key/value pairs\n @param keys Array of data kinds to query\n @param values Array of output pointers corresponding to keys\n @param[out] out_written Number of successfully written values (may be NULL)\n @return GHOSTTY_SUCCESS if every query succeeds, or the first error\n\n @ingroup snapshot"]
    pub fn ghostty_snapshot_decoder_get_multi(
        decoder: GhosttySnapshotDecoder,
        count: usize,
        keys: *const GhosttySnapshotDecoderData,
        values: *mut *mut ::std::os::raw::c_void,
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
