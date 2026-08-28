#![allow(dead_code)]

#[allow(
    dead_code,
    non_camel_case_types,
    non_snake_case,
    non_upper_case_globals,
    clippy::all,
    rustdoc::all
)]
pub mod bindings;

use std::cell::Cell;
use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, HashSet};
use std::ffi::c_void;
use std::fmt;
use std::hash::{Hash, Hasher};
use std::marker::PhantomData;
use std::mem;
use std::ops::RangeInclusive;
use std::os::raw::c_char;
use std::ptr;
use std::slice;
use std::sync::{Mutex, Once, OnceLock};

pub use bindings as ffi;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Error(ffi::GhosttyResult);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FormatterFormat {
    Plain,
    Vt,
}

impl FormatterFormat {
    fn as_raw(self) -> ffi::GhosttyFormatterFormat {
        match self {
            Self::Plain => ffi::GhosttyFormatterFormat_GHOSTTY_FORMATTER_FORMAT_PLAIN,
            Self::Vt => ffi::GhosttyFormatterFormat_GHOSTTY_FORMATTER_FORMAT_VT,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ghostty error {}", self.0)
    }
}

impl std::error::Error for Error {}

trait GhosttyResultExt {
    fn into_result(self) -> Result<(), Error>;
}

impl GhosttyResultExt for ffi::GhosttyResult {
    fn into_result(self) -> Result<(), Error> {
        if self == ffi::GhosttyResult_GHOSTTY_SUCCESS {
            Ok(())
        } else {
            Err(Error(self))
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dirty {
    Clean,
    Partial,
    Full,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RowSelection {
    pub start_x: u16,
    pub end_x: u16,
}

impl RowSelection {
    pub fn range(self) -> RangeInclusive<u16> {
        self.start_x..=self.end_x
    }
}

impl Dirty {
    fn from_raw(value: ffi::GhosttyRenderStateDirty) -> Self {
        match value {
            ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FALSE => Self::Clean,
            ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_PARTIAL => Self::Partial,
            ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FULL => Self::Full,
            _ => Self::Full,
        }
    }

    fn as_raw(self) -> ffi::GhosttyRenderStateDirty {
        match self {
            Self::Clean => ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FALSE,
            Self::Partial => ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_PARTIAL,
            Self::Full => ffi::GhosttyRenderStateDirty_GHOSTTY_RENDER_STATE_DIRTY_FULL,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FocusEvent {
    Gained,
    Lost,
}

impl FocusEvent {
    fn as_raw(self) -> ffi::GhosttyFocusEvent {
        match self {
            Self::Gained => ffi::GhosttyFocusEvent_GHOSTTY_FOCUS_GAINED,
            Self::Lost => ffi::GhosttyFocusEvent_GHOSTTY_FOCUS_LOST,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColorScheme {
    Light,
    Dark,
}

impl ColorScheme {
    fn as_raw(self) -> ffi::GhosttyColorScheme {
        match self {
            Self::Light => ffi::GhosttyColorScheme_GHOSTTY_COLOR_SCHEME_LIGHT,
            Self::Dark => ffi::GhosttyColorScheme_GHOSTTY_COLOR_SCHEME_DARK,
        }
    }
}

pub const MOD_SHIFT: u16 = ffi::GHOSTTY_MODS_SHIFT as u16;
pub const MOD_CTRL: u16 = ffi::GHOSTTY_MODS_CTRL as u16;
pub const MOD_ALT: u16 = ffi::GHOSTTY_MODS_ALT as u16;
pub const MOD_SUPER: u16 = ffi::GHOSTTY_MODS_SUPER as u16;

pub const KEY_ENTER: u32 = ffi::GhosttyKey_GHOSTTY_KEY_ENTER;
pub const KEY_UP: u32 = ffi::GhosttyKey_GHOSTTY_KEY_ARROW_UP;
pub const KEY_DOWN: u32 = ffi::GhosttyKey_GHOSTTY_KEY_ARROW_DOWN;
pub const KEY_LEFT: u32 = ffi::GhosttyKey_GHOSTTY_KEY_ARROW_LEFT;
pub const KEY_RIGHT: u32 = ffi::GhosttyKey_GHOSTTY_KEY_ARROW_RIGHT;
pub const KEY_A: u32 = ffi::GhosttyKey_GHOSTTY_KEY_A;

pub const MOUSE_ACTION_PRESS: ffi::GhosttyMouseAction =
    ffi::GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_PRESS;
pub const MOUSE_ACTION_RELEASE: ffi::GhosttyMouseAction =
    ffi::GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_RELEASE;
pub const MOUSE_ACTION_MOTION: ffi::GhosttyMouseAction =
    ffi::GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_MOTION;
pub const MOUSE_BUTTON_LEFT: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_LEFT;
pub const MOUSE_BUTTON_RIGHT: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_RIGHT;
pub const MOUSE_BUTTON_MIDDLE: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_MIDDLE;
pub const MOUSE_BUTTON_WHEEL_UP: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_FOUR;
pub const MOUSE_BUTTON_WHEEL_DOWN: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_FIVE;
pub const MOUSE_BUTTON_WHEEL_LEFT: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_SIX;
pub const MOUSE_BUTTON_WHEEL_RIGHT: ffi::GhosttyMouseButton =
    ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_SEVEN;
pub const MOUSE_FORMAT_SGR: ffi::GhosttyMouseFormat =
    ffi::GhosttyMouseFormat_GHOSTTY_MOUSE_FORMAT_SGR;

pub const MODE_APPLICATION_CURSOR_KEYS: u16 = 1;
pub const MODE_FOCUS_EVENT: u16 = 1004;
pub const MODE_MOUSE_UTF8: u16 = 1005;
pub const MODE_MOUSE_SGR: u16 = 1006;
pub const MODE_MOUSE_ALTERNATE_SCROLL: u16 = 1007;
pub const MODE_MOUSE_SGR_PIXELS: u16 = 1016;
pub const MODE_BRACKETED_PASTE: u16 = 2004;
pub const MODE_SYNCHRONIZED_OUTPUT: u16 = 2026;
pub const MODE_GRAPHEME_CLUSTER: u16 = 2027;
pub const MODE_COLOR_SCHEME_REPORT: u16 = 2031;
// These are documented in vendor/libghostty-vt/include/ghostty/vt/terminal.h,
// but the generated bindings do not currently expose named constants for them.
const TERMINAL_DATA_COLOR_FOREGROUND: ffi::GhosttyTerminalData = 18;
const TERMINAL_DATA_COLOR_CURSOR: ffi::GhosttyTerminalData = 20;

const KITTY_IMAGE_STORAGE_LIMIT_BYTES: u64 = 64 * 1024 * 1024;
const APC_MAX_BYTES: usize = 16 * 1024 * 1024;
const APC_MAX_BYTES_KITTY: usize = 16 * 1024 * 1024;
pub(crate) const KITTY_UNICODE_PLACEHOLDER: u32 = 0x10EEEE;
// The vendored C headers expose these placement fields, but the checked-in
// generated bindings predate the names. Keep the explicit values aligned with
// vendor/libghostty-vt/include/ghostty/vt/kitty_graphics.h.
const KITTY_PLACEMENT_DATA_IS_VIRTUAL: ffi::GhosttyKittyGraphicsPlacementData = 3;
const KITTY_PLACEMENT_DATA_COLUMNS: ffi::GhosttyKittyGraphicsPlacementData = 10;
const KITTY_PLACEMENT_DATA_ROWS: ffi::GhosttyKittyGraphicsPlacementData = 11;

static INSTALL_PNG_DECODER: Once = Once::new();
static KITTY_PLACEHOLDER_DIACRITICS: OnceLock<HashMap<u32, u32>> = OnceLock::new();

#[derive(Debug, Clone, Copy, Hash, PartialEq, Eq)]
pub enum KittyImageFormat {
    Rgb,
    Rgba,
    Png,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KittyImagePlacement {
    pub image_id: u32,
    pub placement_id: u32,
    pub z: i32,
    pub x_offset: u32,
    pub y_offset: u32,
    pub image_width: u32,
    pub image_height: u32,
    pub format: KittyImageFormat,
    pub data_len: usize,
    pub data_fingerprint: u64,
    pub data: Vec<u8>,
    pub render: KittyPlacementRenderInfo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KittyImageDescriptor {
    pub image_id: u32,
    pub placement_id: u32,
    pub image_width: u32,
    pub image_height: u32,
    pub format: KittyImageFormat,
    pub data_len: usize,
    pub data_fingerprint: u64,
}

#[derive(Debug, Clone, Copy)]
struct KittyImageFingerprintEntry {
    generation: u64,
    fingerprint: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KittyPlacementRenderInfo {
    pub pixel_width: u32,
    pub pixel_height: u32,
    pub grid_cols: u32,
    pub grid_rows: u32,
    pub viewport_col: i32,
    pub viewport_row: i32,
    pub source_x: u32,
    pub source_y: u32,
    pub source_width: u32,
    pub source_height: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct KittyVirtualPlacementSpec {
    image_id: u32,
    placement_id: u32,
    columns: u32,
    rows: u32,
    z: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct KittyVirtualCell {
    x: u16,
    y: u16,
    image_id_low: u32,
    image_id_high: Option<u32>,
    placement_id: Option<u32>,
    row: Option<u32>,
    col: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct KittyVirtualRun {
    x: u16,
    y: u16,
    image_id_low: u32,
    image_id_high: Option<u32>,
    placement_id: Option<u32>,
    row: u32,
    col: u32,
    width: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct KittyVirtualPlacementGeometry {
    x_offset: u32,
    y_offset: u32,
    render: KittyPlacementRenderInfo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CursorVisualStyle {
    Bar,
    Block,
    Underline,
    BlockHollow,
}

impl CursorVisualStyle {
    fn from_raw(value: ffi::GhosttyRenderStateCursorVisualStyle) -> Self {
        match value {
            ffi::GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK => {
                Self::Block
            }
            ffi::GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE => {
                Self::Underline
            }
            ffi::GhosttyRenderStateCursorVisualStyle_GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW => {
                Self::BlockHollow
            }
            _ => Self::Bar,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActiveScreen {
    Primary,
    Alternate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TerminalScrollbar {
    pub total: usize,
    pub offset: usize,
    pub len: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CursorViewport {
    pub x: u16,
    pub y: u16,
    pub wide_tail: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RgbColor {
    pub r: u8,
    pub g: u8,
    pub b: u8,
}

impl From<ffi::GhosttyColorRgb> for RgbColor {
    fn from(value: ffi::GhosttyColorRgb) -> Self {
        Self {
            r: value.r,
            g: value.g,
            b: value.b,
        }
    }
}

pub fn default_palette() -> [RgbColor; 256] {
    let mut palette = [ffi::GhosttyColorRgb::default(); 256];
    unsafe { ffi::ghostty_color_palette_default(palette.as_mut_ptr()) };
    palette.map(Into::into)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CellColor {
    Palette(u8),
    Rgb(RgbColor),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CellStyle {
    pub fg_color: Option<CellColor>,
    pub bg_color: Option<CellColor>,
    pub underline_color: Option<CellColor>,
    pub bold: bool,
    pub italic: bool,
    pub faint: bool,
    pub blink: bool,
    pub inverse: bool,
    pub invisible: bool,
    pub strikethrough: bool,
    pub overline: bool,
    pub underline: u8,
    pub underlined: bool,
}

impl From<ffi::GhosttyStyle> for CellStyle {
    fn from(value: ffi::GhosttyStyle) -> Self {
        Self {
            fg_color: cell_color_from_style_color(value.fg_color),
            bg_color: cell_color_from_style_color(value.bg_color),
            underline_color: cell_color_from_style_color(value.underline_color),
            bold: value.bold,
            italic: value.italic,
            faint: value.faint,
            blink: value.blink,
            inverse: value.inverse,
            invisible: value.invisible,
            strikethrough: value.strikethrough,
            overline: value.overline,
            underline: normalize_underline_style(value.underline),
            underlined: value.underline != 0,
        }
    }
}

fn normalize_underline_style(value: std::os::raw::c_int) -> u8 {
    match value {
        0..=5 => value as u8,
        _ => 1,
    }
}

fn cell_color_from_style_color(color: ffi::GhosttyStyleColor) -> Option<CellColor> {
    match color.tag {
        ffi::GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_PALETTE => {
            // SAFETY: Ghostty's tagged union stores `palette` when the tag is PALETTE.
            Some(CellColor::Palette(unsafe { color.value.palette }))
        }
        ffi::GhosttyStyleColorTag_GHOSTTY_STYLE_COLOR_RGB => {
            // SAFETY: Ghostty's tagged union stores `rgb` when the tag is RGB.
            Some(CellColor::Rgb(unsafe { color.value.rgb }.into()))
        }
        _ => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RenderColors {
    pub background: RgbColor,
    pub foreground: RgbColor,
    pub palette: [RgbColor; 256],
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CellWide {
    Narrow,
    Wide,
    SpacerTail,
    SpacerHead,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ScreenTextCell {
    pub wide: CellWide,
    pub graphemes: Vec<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ScreenTextRow {
    pub cells: Vec<ScreenTextCell>,
    pub soft_wrapped: bool,
    pub wrap_continuation: bool,
}

impl CellWide {
    fn from_raw(value: ffi::GhosttyCellWide) -> Self {
        match value {
            ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_NARROW => Self::Narrow,
            ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_WIDE => Self::Wide,
            ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_SPACER_TAIL => Self::SpacerTail,
            ffi::GhosttyCellWide_GHOSTTY_CELL_WIDE_SPACER_HEAD => Self::SpacerHead,
            _ => Self::Narrow,
        }
    }
}

type WritePtyCallback = dyn FnMut(&[u8]) + Send;

const MAX_CLIPBOARD_BYTES: usize = 192 * 1024;

#[derive(Default)]
struct TerminalCallbackState {
    write_pty: Option<Box<WritePtyCallback>>,
    pwd_changes: Vec<Vec<u8>>,
    clipboard_writes: Vec<Vec<u8>>,
    size_report: ffi::GhosttySizeReportSize,
    color_scheme: Option<ColorScheme>,
}

unsafe extern "C" fn color_scheme_trampoline(
    _terminal: ffi::GhosttyTerminal,
    userdata: *mut c_void,
    out_scheme: *mut ffi::GhosttyColorScheme,
) -> bool {
    if userdata.is_null() || out_scheme.is_null() {
        return false;
    }
    let state = unsafe { &*userdata.cast::<TerminalCallbackState>() };
    let Some(color_scheme) = state.color_scheme else {
        return false;
    };
    unsafe {
        out_scheme.write(color_scheme.as_raw());
    }
    true
}

unsafe extern "C" fn size_trampoline(
    _terminal: ffi::GhosttyTerminal,
    userdata: *mut c_void,
    out_size: *mut ffi::GhosttySizeReportSize,
) -> bool {
    if userdata.is_null() || out_size.is_null() {
        return false;
    }
    let state = unsafe { &*userdata.cast::<TerminalCallbackState>() };
    let size = state.size_report;
    if size.rows == 0 || size.columns == 0 || size.cell_width == 0 || size.cell_height == 0 {
        return false;
    }
    unsafe {
        out_size.write(size);
    }
    true
}

unsafe extern "C" fn write_pty_trampoline(
    _terminal: ffi::GhosttyTerminal,
    userdata: *mut c_void,
    data: *const u8,
    len: usize,
) {
    if userdata.is_null() || (data.is_null() && len != 0) {
        return;
    }
    let state = unsafe { &mut *(userdata.cast::<TerminalCallbackState>()) };
    let Some(callback) = state.write_pty.as_mut() else {
        return;
    };
    let bytes = if len == 0 {
        &[]
    } else {
        unsafe { slice::from_raw_parts(data, len) }
    };
    callback(bytes);
}

unsafe extern "C" fn clipboard_write_trampoline(
    _terminal: ffi::GhosttyTerminal,
    userdata: *mut c_void,
    write: *const ffi::GhosttyClipboardWrite,
) -> ffi::GhosttyClipboardWriteResult {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        // SAFETY: libghostty-vt owns these values for the synchronous callback.
        unsafe { capture_clipboard_write(userdata, write) }
    }))
    .unwrap_or(ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA)
}

unsafe fn capture_clipboard_write(
    userdata: *mut c_void,
    write: *const ffi::GhosttyClipboardWrite,
) -> ffi::GhosttyClipboardWriteResult {
    if userdata.is_null() || write.is_null() {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    }

    let required_size = std::mem::offset_of!(ffi::GhosttyClipboardWrite, contents_len)
        + std::mem::size_of::<usize>();
    // SAFETY: size is the leading field of the live request.
    if unsafe { (*write).size } < required_size {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    }
    // SAFETY: the size check covers every field accessed below.
    let request = unsafe { &*write };
    if request.location != ffi::GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_STANDARD {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    }

    // SAFETY: userdata is the TerminalCallbackState installed with this terminal.
    let state = unsafe { &mut *userdata.cast::<TerminalCallbackState>() };
    if request.contents_len == 0 {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS;
    }
    if request.contents_len != 1 {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    }
    if request.contents.is_null() {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    }

    // SAFETY: libghostty-vt keeps the single content and its strings alive for the callback.
    let content = unsafe { &*request.contents };
    // SAFETY: the MIME string is borrowed from the live callback request.
    let Some(mime) = (unsafe { borrowed_bytes(content.mime) }) else {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    };
    let is_text = std::str::from_utf8(mime)
        .ok()
        .and_then(|mime| mime.split(';').next())
        .is_some_and(|mime| mime.trim().eq_ignore_ascii_case("text/plain"));
    if !is_text {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    }

    // SAFETY: the data string is borrowed from the live callback request.
    let Some(bytes) = (unsafe { borrowed_bytes(content.data) }) else {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    };
    if bytes.is_empty() {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    }
    if bytes.len() > MAX_CLIPBOARD_BYTES {
        return ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    }
    state.clipboard_writes.push(bytes.to_vec());
    ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS
}

unsafe fn borrowed_bytes<'a>(value: ffi::GhosttyString) -> Option<&'a [u8]> {
    if value.len == 0 {
        Some(&[])
    } else if value.ptr.is_null() {
        None
    } else {
        // SAFETY: the callback contract keeps pointer and length valid until return.
        Some(unsafe { slice::from_raw_parts(value.ptr, value.len) })
    }
}

unsafe extern "C" fn pwd_changed_trampoline(terminal: ffi::GhosttyTerminal, userdata: *mut c_void) {
    if terminal.is_null() || userdata.is_null() {
        return;
    }
    let mut pwd = ffi::GhosttyString::default();
    let result = unsafe {
        ffi::ghostty_terminal_get(
            terminal,
            ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_PWD,
            (&mut pwd as *mut ffi::GhosttyString).cast(),
        )
    };
    if result != ffi::GhosttyResult_GHOSTTY_SUCCESS || (pwd.ptr.is_null() && pwd.len != 0) {
        return;
    }
    let bytes = if pwd.len == 0 {
        Vec::new()
    } else {
        unsafe { slice::from_raw_parts(pwd.ptr, pwd.len) }.to_vec()
    };
    let state = unsafe { &mut *(userdata.cast::<TerminalCallbackState>()) };
    state.pwd_changes.push(bytes);
}

fn install_png_decoder_once() {
    INSTALL_PNG_DECODER.call_once(|| unsafe {
        let _ = ffi::ghostty_sys_set(
            ffi::GhosttySysOption_GHOSTTY_SYS_OPT_DECODE_PNG,
            (decode_png_trampoline as *const ()).cast(),
        );
    });
}

unsafe extern "C" fn decode_png_trampoline(
    _userdata: *mut c_void,
    allocator: *const ffi::GhosttyAllocator,
    data: *const u8,
    data_len: usize,
    out: *mut ffi::GhosttySysImage,
) -> bool {
    if data.is_null() || out.is_null() {
        return false;
    }
    let bytes = unsafe { slice::from_raw_parts(data, data_len) };
    let Some(rgba) = decode_png_rgba(bytes) else {
        return false;
    };
    let ptr = unsafe { ffi::ghostty_alloc(allocator, rgba.data.len()) };
    if ptr.is_null() {
        return false;
    }
    unsafe {
        ptr::copy_nonoverlapping(rgba.data.as_ptr(), ptr, rgba.data.len());
        *out = ffi::GhosttySysImage {
            width: rgba.width,
            height: rgba.height,
            data: ptr,
            data_len: rgba.data.len(),
        };
    }
    true
}

struct DecodedPng {
    width: u32,
    height: u32,
    data: Vec<u8>,
}

fn decode_png_rgba(bytes: &[u8]) -> Option<DecodedPng> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().ok()?;
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).ok()?;
    let frame = &buf[..info.buffer_size()];
    if info.bit_depth != png::BitDepth::Eight {
        return None;
    }

    let data = match info.color_type {
        png::ColorType::Rgba => frame.to_vec(),
        png::ColorType::Rgb => {
            let mut out = Vec::with_capacity((info.width as usize) * (info.height as usize) * 4);
            for rgb in frame.chunks_exact(3) {
                out.extend_from_slice(&[rgb[0], rgb[1], rgb[2], 255]);
            }
            out
        }
        png::ColorType::Grayscale => {
            let mut out = Vec::with_capacity((info.width as usize) * (info.height as usize) * 4);
            for gray in frame {
                out.extend_from_slice(&[*gray, *gray, *gray, 255]);
            }
            out
        }
        png::ColorType::GrayscaleAlpha => {
            let mut out = Vec::with_capacity((info.width as usize) * (info.height as usize) * 4);
            for ga in frame.chunks_exact(2) {
                out.extend_from_slice(&[ga[0], ga[0], ga[0], ga[1]]);
            }
            out
        }
        png::ColorType::Indexed => return None,
    };

    Some(DecodedPng {
        width: info.width,
        height: info.height,
        data,
    })
}

pub fn unicode_codepoint_width(codepoint: u32) -> u8 {
    unsafe { ffi::ghostty_unicode_codepoint_width(codepoint) }
}

pub fn unicode_grapheme_width(codepoints: &[u32]) -> (usize, u8) {
    let mut width = 0u8;
    let consumed = unsafe {
        ffi::ghostty_unicode_grapheme_width(codepoints.as_ptr(), codepoints.len(), &mut width)
    };
    (consumed, width)
}

pub fn encode_focus(event: FocusEvent) -> Result<Vec<u8>, Error> {
    let mut required = 0usize;
    // SAFETY: null buffer + out len is the documented way to query required size.
    let result =
        unsafe { ffi::ghostty_focus_encode(event.as_raw(), ptr::null_mut(), 0, &mut required) };
    if result != ffi::GhosttyResult_GHOSTTY_OUT_OF_SPACE {
        result.into_result()?;
    }

    let mut buffer = vec![0u8; required];
    // SAFETY: buffer is allocated for required size; function writes at most that many bytes.
    unsafe {
        ffi::ghostty_focus_encode(
            event.as_raw(),
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut required,
        )
        .into_result()?;
    }
    buffer.truncate(required);
    Ok(buffer)
}

pub struct Terminal {
    raw: ffi::GhosttyTerminal,
    callback_state: Box<TerminalCallbackState>,
    kitty_fingerprints: Mutex<HashMap<u32, KittyImageFingerprintEntry>>,
    kitty_empty_generation: Cell<Option<u64>>,
}

include!("terminal_api.rs");
include!("terminal_ops.rs");
include!("render_pre.rs");
include!("render_state.rs");
#[cfg(test)]
#[path = "mod/tests.rs"]
mod tests;
