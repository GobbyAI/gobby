use std::io::Read;

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};

#[path = "raw_input_framer.rs"]
mod raw_input_framer;
use raw_input_framer::*;

/// Parse raw terminal input bytes into a list of `RawInputEvent`s.
///
/// This is used by the headless server to route client input through the
/// same parsing pipeline that the monolithic binary uses for stdin.
/// Incomplete sequences at the end of the buffer are flushed as best-effort
/// (same logic as the live input reader).
#[allow(dead_code)]
pub fn parse_raw_input_bytes(data: &[u8]) -> Vec<RawInputEvent> {
    // Delegate to the sync version which actually works.
    parse_raw_input_bytes_sync(data)
}

/// A raw input event paired with the byte range it consumed from the original buffer.
#[cfg(test)]
#[derive(Debug)]
pub struct RawInputEventWithRange {
    /// The parsed event.
    pub event: RawInputEvent,
    /// Byte offset where this event starts in the original buffer.
    pub start: usize,
    /// Number of bytes this event consumed from the original buffer.
    /// For events generated from flushed incomplete bytes, `len` may be 0
    /// (synthetic events that don't map to original bytes).
    pub len: usize,
}

/// Parse raw terminal input bytes into a list of `RawInputEventWithRange`s (synchronous version).
///
/// Unlike `parse_raw_input_bytes_sync`, this preserves the byte offset for each
/// event, allowing callers to write only the specific bytes for each event
/// instead of the entire input buffer.
#[cfg(test)]
pub fn parse_raw_input_bytes_with_ranges(data: &[u8]) -> Vec<RawInputEventWithRange> {
    let mut buffer = data.to_vec();
    let mut events = Vec::new();
    let mut offset = 0usize;

    while let Some((event, consumed)) = extract_one_event(&buffer) {
        buffer.drain(..consumed);
        events.push(RawInputEventWithRange {
            event,
            start: offset,
            len: consumed,
        });
        offset += consumed;
    }

    // Flush remaining incomplete bytes.
    if !buffer.is_empty() {
        if buffer.as_slice() == [ESC] {
            events.push(RawInputEventWithRange {
                event: RawInputEvent::Key(
                    TerminalKey::new(crossterm::event::KeyCode::Esc, KeyModifiers::empty())
                        .with_vt_bytes(vec![ESC]),
                ),
                start: offset,
                len: 1,
            });
        } else if matches!(
            control_string(&buffer),
            Some(ControlString::Incomplete { .. })
        ) {
            return events;
        } else if let Ok(text) = std::str::from_utf8(&buffer) {
            if let Some(key) = parse_terminal_key_sequence(text) {
                events.push(RawInputEventWithRange {
                    event: RawInputEvent::Key(key.with_text_commit().with_vt_bytes(buffer.clone())),
                    start: offset,
                    len: buffer.len(),
                });
            }
        }
    }

    events
}

/// Parse raw terminal input bytes into a list of `RawInputEvent`s (synchronous version).
///
/// Unlike `parse_raw_input_bytes`, this directly extracts events without
/// going through a channel, making it suitable for synchronous use.
pub fn parse_raw_input_bytes_sync(data: &[u8]) -> Vec<RawInputEvent> {
    let mut framer = RawInputFramer::default();
    let mut events = framer.push(data);
    events.extend(framer.flush_timeout());
    events
}

#[cfg(unix)]
use std::os::fd::AsRawFd;
use tokio::sync::mpsc;

use crate::input::{parse_terminal_key_sequence, TerminalKey, TextCommit};
use crate::terminal_theme::{
    parse_default_color_response, parse_palette_color_response, DefaultColorKind, HostAppearance,
    RgbColor,
};

const ESC: u8 = 0x1b;
pub(crate) const RAW_INPUT_IDLE_FLUSH_TIMEOUT_MS: i32 = 10;
pub(crate) const MOUSE_ACTIVE_ESCAPE_SEQUENCE_FLUSH_TIMEOUT_MS: i32 = 150;
pub(crate) const GHOSTTY_COLOR_SCHEME_DARK_REPORT: &[u8] = b"\x1b[?997;1n";
pub(crate) const GHOSTTY_COLOR_SCHEME_LIGHT_REPORT: &[u8] = b"\x1b[?997;2n";
const BRACKETED_PASTE_START: &[u8] = b"\x1b[200~";
const BRACKETED_PASTE_END: &[u8] = b"\x1b[201~";

/// Returns whether `data` is exactly one complete bracketed-paste sequence.
///
/// Client transport uses this to distinguish recoverable oversized interactive
/// pastes from generic oversized input, which remains a protocol violation.
pub(crate) fn is_complete_text_bracketed_paste(data: &[u8]) -> bool {
    if !data.starts_with(BRACKETED_PASTE_START) {
        return false;
    }
    let Some(end) = find_subsequence(data, BRACKETED_PASTE_END) else {
        return false;
    };
    end + BRACKETED_PASTE_END.len() == data.len()
        && std::str::from_utf8(&data[BRACKETED_PASTE_START.len()..end]).is_ok()
}

#[derive(Debug)]
pub enum RawInputEvent {
    Key(TerminalKey),
    Text(TextCommit),
    Paste(String),
    Mouse(MouseEvent),
    OuterFocusGained,
    OuterFocusLost,
    HostDefaultColor {
        kind: DefaultColorKind,
        color: RgbColor,
    },
    HostPaletteColors {
        colors: Vec<(u8, RgbColor)>,
    },
    HostColorSchemeChanged(HostAppearance),
    // The dimensions are only read by the Unix client.
    #[cfg_attr(not(any(unix, test)), allow(dead_code))]
    HostCellSizeReport {
        width_px: u32,
        height_px: u32,
    },
    Unsupported,
}

fn plausible_control_string_tail(family: ControlStringFamily, buffer: &[u8]) -> bool {
    match family {
        ControlStringFamily::Osc => buffer.iter().all(|byte| {
            byte.is_ascii_digit()
                || matches!(
                    *byte,
                    b';' | b':'
                        | b'/'
                        | b'#'
                        | b'?'
                        | b'.'
                        | b'_'
                        | b'-'
                        | b'+'
                        | b'r'
                        | b'g'
                        | b'b'
                        | b'R'
                        | b'G'
                        | b'B'
                        | ESC
                )
        }),
        ControlStringFamily::StTerminated => buffer.last() == Some(&ESC),
        ControlStringFamily::HostReplyCsi => false,
        ControlStringFamily::OrphanedSgrMouseTail => buffer
            .iter()
            .all(|byte| byte.is_ascii_digit() || matches!(*byte, b';' | b'M' | b'm')),
    }
}

pub(crate) fn events_require_host_surface_redraw(
    events: &[RawInputEvent],
    redraw_on_focus_gained: bool,
) -> bool {
    redraw_on_focus_gained
        && events
            .iter()
            .any(|event| matches!(event, RawInputEvent::OuterFocusGained))
}

#[cfg(any(not(windows), test))]
pub(crate) fn events_require_host_terminal_theme_query(events: &[RawInputEvent]) -> bool {
    events
        .iter()
        .any(|event| matches!(event, RawInputEvent::HostColorSchemeChanged(_)))
}

fn input_flush_timeout_ms(framer: &RawInputFramer) -> i32 {
    if framer.has_pending_incomplete_sgr_mouse_sequence() {
        MOUSE_ACTIVE_ESCAPE_SEQUENCE_FLUSH_TIMEOUT_MS
    } else {
        RAW_INPUT_IDLE_FLUSH_TIMEOUT_MS
    }
}

pub fn spawn_input_reader() -> mpsc::Receiver<RawInputEvent> {
    let (tx, rx) = mpsc::channel(256);

    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        let mut reader = stdin.lock();
        let mut scratch = [0u8; 1024];
        let mut framer = RawInputFramer::for_host_input();
        framer.host_color_query_sent();
        framer.enable_host_color_scheme_change_tracking();
        let mut pending_palette = Vec::new();

        loop {
            match reader.read(&mut scratch) {
                Ok(0) => break,
                Ok(n) => {
                    send_raw_input_events(framer.push(&scratch[..n]), &tx, &mut pending_palette);

                    if stdin_read_ready(&reader, input_flush_timeout_ms(&framer)) == Some(false) {
                        let had_pending = framer.has_pending_input();
                        let events = framer.flush_timeout();
                        let held_escape = had_pending && events.is_empty();
                        send_raw_input_events(events, &tx, &mut pending_palette);
                        flush_host_palette_events(&tx, &mut pending_palette);
                        if held_escape
                            && stdin_read_ready(&reader, RAW_INPUT_IDLE_FLUSH_TIMEOUT_MS)
                                == Some(false)
                        {
                            send_raw_input_events(
                                framer.flush_timeout(),
                                &tx,
                                &mut pending_palette,
                            );
                            flush_host_palette_events(&tx, &mut pending_palette);
                        }
                    }
                }
                Err(_) => break,
            }
        }
    });

    rx
}

fn send_raw_input_events(
    events: Vec<RawInputEvent>,
    tx: &mpsc::Sender<RawInputEvent>,
    pending_palette: &mut Vec<(u8, RgbColor)>,
) {
    for event in events {
        match event {
            RawInputEvent::HostPaletteColors { colors } => {
                pending_palette.extend(colors);
                if pending_palette.len() == 256 {
                    flush_host_palette_events(tx, pending_palette);
                }
            }
            event @ RawInputEvent::HostDefaultColor { .. } => {
                let _ = tx.blocking_send(event);
            }
            event => {
                flush_host_palette_events(tx, pending_palette);
                let _ = tx.blocking_send(event);
            }
        }
    }
}

fn flush_host_palette_events(
    tx: &mpsc::Sender<RawInputEvent>,
    pending_palette: &mut Vec<(u8, RgbColor)>,
) {
    if pending_palette.is_empty() {
        return;
    }
    let colors = std::mem::take(pending_palette);
    let _ = tx.blocking_send(RawInputEvent::HostPaletteColors { colors });
}

#[cfg(test)]
fn drain_buffer(buffer: &mut Vec<u8>, tx: &mpsc::Sender<RawInputEvent>) {
    for bytes in drain_complete_input_bytes(buffer) {
        let Some((event, _consumed)) = extract_one_event(&bytes) else {
            continue;
        };
        tracing::debug!(raw_bytes = ?bytes, event = ?event, "raw input event parsed");
        let _ = tx.blocking_send(event);
    }
}

#[cfg(test)]
pub(crate) fn drain_complete_input_bytes(buffer: &mut Vec<u8>) -> Vec<Vec<u8>> {
    let mut chunks = Vec::new();

    while let Some((_event, consumed)) = extract_one_event(buffer) {
        chunks.push(buffer[..consumed].to_vec());
        buffer.drain(..consumed);
    }

    chunks
}

#[cfg(test)]
fn flush_incomplete_buffer(buffer: &mut Vec<u8>, tx: &mpsc::Sender<RawInputEvent>) {
    if let Some(bytes) = flush_incomplete_input_bytes(buffer) {
        if bytes.as_slice() == [ESC] {
            let _ = tx.blocking_send(RawInputEvent::Key(
                TerminalKey::new(crossterm::event::KeyCode::Esc, KeyModifiers::empty())
                    .with_vt_bytes(bytes),
            ));
            return;
        }

        let Some((event, _consumed)) = extract_one_event(&bytes) else {
            return;
        };
        let _ = tx.blocking_send(event);
    }
}

#[cfg(test)]
pub(crate) fn flush_incomplete_input_bytes(buffer: &mut Vec<u8>) -> Option<Vec<u8>> {
    let mut framer = RawInputByteFramer {
        buffer: std::mem::take(buffer),
        ..Default::default()
    };
    let mut chunks = framer.flush_timeout();
    *buffer = framer.buffer;
    chunks.pop()
}

#[cfg(unix)]
fn stdin_read_ready<R: AsRawFd>(_reader: &R, _timeout_ms: i32) -> Option<bool> {
    #[cfg(unix)]
    {
        let fd = _reader.as_raw_fd();
        poll_read_ready(fd, _timeout_ms)
    }
}

#[cfg(not(unix))]
fn stdin_read_ready<R>(_reader: &R, _timeout_ms: i32) -> Option<bool> {
    None
}

#[cfg(unix)]
fn poll_read_ready(fd: i32, timeout_ms: i32) -> Option<bool> {
    #[repr(C)]
    struct PollFd {
        fd: i32,
        events: i16,
        revents: i16,
    }

    unsafe extern "C" {
        fn poll(fds: *mut PollFd, nfds: usize, timeout: i32) -> i32;
    }

    const POLLIN: i16 = 0x0001;

    let mut pfd = PollFd {
        fd,
        events: POLLIN,
        revents: 0,
    };

    let result = unsafe { poll(&mut pfd as *mut PollFd, 1, timeout_ms) };
    if result < 0 {
        None
    } else {
        Some(result > 0)
    }
}

fn extract_one_event(buffer: &[u8]) -> Option<(RawInputEvent, usize)> {
    if buffer.is_empty() {
        return None;
    }

    if buffer.starts_with(BRACKETED_PASTE_START) {
        let end = find_subsequence(buffer, BRACKETED_PASTE_END)?;
        let content = std::str::from_utf8(&buffer[BRACKETED_PASTE_START.len()..end]).ok()?;
        return Some((
            RawInputEvent::Paste(content.to_string()),
            end + BRACKETED_PASTE_END.len(),
        ));
    }

    if buffer[0] == ESC {
        let seq_len = complete_escape_sequence_len(buffer)?;
        let seq = std::str::from_utf8(&buffer[..seq_len]).ok()?;

        if let Some((kind, color)) = parse_default_color_response(seq) {
            return Some((RawInputEvent::HostDefaultColor { kind, color }, seq_len));
        }
        if let Some((index, color)) = parse_palette_color_response(seq) {
            return Some((
                RawInputEvent::HostPaletteColors {
                    colors: vec![(index, color)],
                },
                seq_len,
            ));
        }

        match seq {
            "\x1b[I" => return Some((RawInputEvent::OuterFocusGained, seq_len)),
            "\x1b[O" => return Some((RawInputEvent::OuterFocusLost, seq_len)),
            _ => {}
        }

        if let Some(appearance) = parse_host_color_scheme_report(&buffer[..seq_len]) {
            return Some((RawInputEvent::HostColorSchemeChanged(appearance), seq_len));
        }

        if let Some((width_px, height_px)) = parse_host_cell_size_report(&buffer[..seq_len]) {
            return Some((
                RawInputEvent::HostCellSizeReport {
                    width_px,
                    height_px,
                },
                seq_len,
            ));
        }

        if let Some(mouse) = parse_sgr_mouse(seq) {
            return Some((RawInputEvent::Mouse(mouse), seq_len));
        }

        if let Some(key) = parse_terminal_key_sequence(seq) {
            return Some((
                RawInputEvent::Key(key.with_vt_bytes(buffer[..seq_len].to_vec())),
                seq_len,
            ));
        }

        tracing::debug!(sequence = ?seq, "dropping unsupported escape sequence");
        return Some((RawInputEvent::Unsupported, seq_len));
    }

    let consumed = first_complete_utf8_char_len(buffer)?;
    let text = std::str::from_utf8(&buffer[..consumed]).ok()?;
    let key = parse_terminal_key_sequence(text)?
        .with_text_commit()
        .with_vt_bytes(buffer[..consumed].to_vec());
    Some((RawInputEvent::Key(key), consumed))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ControlStringFamily {
    Osc,
    StTerminated,
    HostReplyCsi,
    OrphanedSgrMouseTail,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ControlString {
    Complete {
        len: usize,
        family: ControlStringFamily,
    },
    Incomplete {
        family: ControlStringFamily,
    },
}

fn parse_host_color_scheme_report(buffer: &[u8]) -> Option<HostAppearance> {
    match buffer {
        GHOSTTY_COLOR_SCHEME_DARK_REPORT => Some(HostAppearance::Dark),
        GHOSTTY_COLOR_SCHEME_LIGHT_REPORT => Some(HostAppearance::Light),
        _ => None,
    }
}

/// Parses an XTWINOPS cell size report (`CSI 6 ; height ; width t`) into
/// `(width_px, height_px)`; note the reply orders height first.
fn parse_host_cell_size_report(buffer: &[u8]) -> Option<(u32, u32)> {
    let body = buffer.strip_prefix(b"\x1b[")?.strip_suffix(b"t")?;
    let text = std::str::from_utf8(body).ok()?;
    let mut params = text.split(';');
    if params.next()? != "6" {
        return None;
    }
    let height_px = params.next()?.parse::<u32>().ok()?;
    let width_px = params.next()?.parse::<u32>().ok()?;
    if params.next().is_some() || width_px == 0 || height_px == 0 {
        return None;
    }
    Some((width_px, height_px))
}

fn starts_with_incomplete_default_color_response(buffer: &[u8]) -> bool {
    matches!(
        control_string(buffer),
        Some(ControlString::Incomplete {
            family: ControlStringFamily::Osc
        })
    ) && matches!(buffer.get(..5), Some(b"\x1b]10;" | b"\x1b]11;"))
}

fn starts_with_incomplete_host_color_scheme_report(buffer: &[u8]) -> bool {
    buffer.starts_with(b"\x1b[?")
        && (GHOSTTY_COLOR_SCHEME_DARK_REPORT.starts_with(buffer)
            || GHOSTTY_COLOR_SCHEME_LIGHT_REPORT.starts_with(buffer))
        && buffer.len() < GHOSTTY_COLOR_SCHEME_DARK_REPORT.len()
}

fn starts_with_incomplete_host_cell_size_report(buffer: &[u8]) -> bool {
    let Some(body) = buffer.strip_prefix(b"\x1b[") else {
        return false;
    };
    if body.is_empty() || body.last() == Some(&b't') {
        return false;
    }

    let mut params = body.split(|byte| *byte == b';');
    if params.next() != Some(b"6".as_slice()) {
        return false;
    }
    let height = params.next();
    let width = params.next();
    params.next().is_none()
        && height.is_none_or(|value| value.iter().all(u8::is_ascii_digit))
        && width.is_none_or(|value| value.iter().all(u8::is_ascii_digit))
        && !(height.is_some_and(<[u8]>::is_empty) && width.is_some())
}

fn control_string(buffer: &[u8]) -> Option<ControlString> {
    let family = match buffer.get(..2)? {
        b"\x1b]" => ControlStringFamily::Osc,
        b"\x1bP" | b"\x1b_" | b"\x1b^" | b"\x1bX" => ControlStringFamily::StTerminated,
        _ => return None,
    };

    Some(match control_string_terminator_for_family(buffer, family) {
        Some(len) => ControlString::Complete { len, family },
        None => ControlString::Incomplete { family },
    })
}

fn first_complete_utf8_char_len(buffer: &[u8]) -> Option<usize> {
    let width = utf8_char_width(*buffer.first()?)?;

    if buffer.len() < width {
        return None;
    }

    std::str::from_utf8(&buffer[..width]).ok()?;
    Some(width)
}

fn starts_with_incomplete_utf8_char(buffer: &[u8]) -> bool {
    match std::str::from_utf8(buffer) {
        Ok(_) => false,
        Err(err) => err.valid_up_to() == 0 && err.error_len().is_none(),
    }
}

fn utf8_char_width(first: u8) -> Option<usize> {
    if first < 0x80 {
        Some(1)
    } else if first & 0b1110_0000 == 0b1100_0000 {
        Some(2)
    } else if first & 0b1111_0000 == 0b1110_0000 {
        Some(3)
    } else if first & 0b1111_1000 == 0b1111_0000 {
        Some(4)
    } else {
        None
    }
}

fn complete_escape_sequence_len(buffer: &[u8]) -> Option<usize> {
    if buffer.len() == 1 {
        return None;
    }

    if buffer.starts_with(b"\x1b\x1b[<") {
        if let Some(mouse_len) = find_csi_final(&buffer[1..], b"Mm") {
            let mouse_sequence = std::str::from_utf8(&buffer[1..1 + mouse_len]).ok()?;
            if parse_sgr_mouse(mouse_sequence).is_some() {
                return Some(1);
            }
        }
    }

    if buffer.starts_with(b"\x1b\x1b") {
        return complete_escape_sequence_len(&buffer[1..]).map(|len| len + 1);
    }

    if buffer.starts_with(b"\x1b[") {
        if buffer.starts_with(b"\x1b[<") {
            return find_csi_final(buffer, b"Mm");
        }
        return find_csi_final(
            buffer,
            b"@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~",
        );
    }

    if let Some(control) = control_string(buffer) {
        return match control {
            ControlString::Complete { len, .. } => Some(len),
            ControlString::Incomplete { .. } => None,
        };
    }

    if buffer.starts_with(b"\x1bO") {
        return (buffer.len() >= 3).then_some(3);
    }

    let escaped_char_width = utf8_char_width(buffer[1])?;
    if buffer.len() < 1 + escaped_char_width {
        return None;
    }
    std::str::from_utf8(&buffer[1..1 + escaped_char_width]).ok()?;
    Some(1 + escaped_char_width)
}

fn starts_with_incomplete_sgr_mouse_sequence(buffer: &[u8]) -> bool {
    buffer.starts_with(b"\x1b[<")
        && buffer[3..]
            .iter()
            .all(|byte| byte.is_ascii_digit() || *byte == b';')
}

fn starts_with_incomplete_orphaned_sgr_mouse_tail(buffer: &[u8]) -> bool {
    if buffer.len() > MAX_ORPHANED_SGR_MOUSE_TAIL_BYTES {
        return false;
    }
    buffer.len() < 3 && b"[<".starts_with(buffer)
        || buffer.starts_with(b"[<")
            && buffer[2..]
                .iter()
                .all(|byte| byte.is_ascii_digit() || *byte == b';')
}

fn discard_complete_orphaned_sgr_mouse_tail(buffer: &mut Vec<u8>) -> bool {
    let Some(terminator_len) =
        control_string_terminator_for_family(buffer, ControlStringFamily::OrphanedSgrMouseTail)
    else {
        return false;
    };
    if terminator_len > MAX_ORPHANED_SGR_MOUSE_TAIL_BYTES {
        return false;
    }
    let mut sequence = Vec::with_capacity(terminator_len + 1);
    sequence.push(ESC);
    sequence.extend_from_slice(&buffer[..terminator_len]);
    let Ok(sequence) = std::str::from_utf8(&sequence) else {
        return false;
    };
    if parse_sgr_mouse(sequence).is_none() {
        return false;
    }
    buffer.drain(..terminator_len);
    true
}

fn discard_or_buffer_orphaned_sgr_mouse_tail(
    buffer: &mut Vec<u8>,
    discard_until: &mut Option<ControlStringFamily>,
    discarded_tail_bytes: &mut usize,
) {
    if !discard_complete_orphaned_sgr_mouse_tail(buffer) {
        *discarded_tail_bytes = buffer.len();
        *discard_until = (*discarded_tail_bytes <= MAX_DISCARDED_CONTROL_TAIL_BYTES)
            .then_some(ControlStringFamily::OrphanedSgrMouseTail);
        buffer.clear();
    }
}

fn discard_host_reply_csi_tail(buffer: &mut Vec<u8>, discarded_tail_bytes: &mut usize) -> bool {
    let remaining = MAX_DISCARDED_CONTROL_TAIL_BYTES.saturating_sub(*discarded_tail_bytes);
    let inspected = buffer.len().min(remaining);

    for index in 0..inspected {
        match buffer[index] {
            0x20..=0x3f => {}
            0x40..=0x7e => {
                buffer.drain(..=index);
                return true;
            }
            _ => {
                buffer.drain(..index);
                return true;
            }
        }
    }

    buffer.drain(..inspected);
    *discarded_tail_bytes = discarded_tail_bytes.saturating_add(inspected);
    *discarded_tail_bytes >= MAX_DISCARDED_CONTROL_TAIL_BYTES
}

fn discard_orphaned_sgr_mouse_tail(buffer: &mut Vec<u8>, discarded_tail_bytes: &mut usize) -> bool {
    let remaining = MAX_DISCARDED_CONTROL_TAIL_BYTES.saturating_sub(*discarded_tail_bytes);
    let inspected = buffer.len().min(remaining);

    for index in 0..inspected {
        match buffer[index] {
            b'0'..=b'9' | b';' => {}
            b'M' | b'm' => {
                buffer.drain(..=index);
                return true;
            }
            _ => {
                buffer.drain(..index);
                return true;
            }
        }
    }

    *discarded_tail_bytes = discarded_tail_bytes.saturating_add(inspected);
    if buffer.len() > inspected {
        buffer.clear();
        return true;
    }

    buffer.clear();
    false
}

fn osc_string_terminator(buffer: &[u8]) -> Option<usize> {
    let st = find_subsequence(buffer, b"\x1b\\").map(|idx| idx + 2);
    let bel = buffer
        .iter()
        .position(|byte| *byte == b'\x07')
        .map(|idx| idx + 1);

    match (st, bel) {
        (Some(st), Some(bel)) => Some(st.min(bel)),
        (Some(st), None) => Some(st),
        (None, Some(bel)) => Some(bel),
        (None, None) => None,
    }
}

fn st_string_terminator(buffer: &[u8]) -> Option<usize> {
    find_subsequence(buffer, b"\x1b\\").map(|idx| idx + 2)
}

fn control_string_terminator_for_family(
    buffer: &[u8],
    family: ControlStringFamily,
) -> Option<usize> {
    match family {
        ControlStringFamily::Osc => osc_string_terminator(buffer),
        ControlStringFamily::StTerminated => st_string_terminator(buffer),
        ControlStringFamily::HostReplyCsi => None,
        ControlStringFamily::OrphanedSgrMouseTail => buffer
            .iter()
            .position(|byte| matches!(*byte, b'M' | b'm'))
            .map(|idx| idx + 1),
    }
}

fn find_csi_final(buffer: &[u8], finals: &[u8]) -> Option<usize> {
    for (idx, byte) in buffer.iter().enumerate().skip(2) {
        if finals.contains(byte) {
            return Some(idx + 1);
        }
    }
    None
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn parse_sgr_mouse(sequence: &str) -> Option<MouseEvent> {
    let body = sequence.strip_prefix("\x1b[<")?;
    let final_char = body.chars().last()?;
    if final_char != 'M' && final_char != 'm' {
        return None;
    }

    let payload = &body[..body.len() - 1];
    let mut parts = payload.split(';');
    let cb = parts.next()?.parse::<u8>().ok()?;
    let column = parts.next()?.parse::<u16>().ok()?.checked_sub(1)?;
    let row = parts.next()?.parse::<u16>().ok()?.checked_sub(1)?;
    let (kind, modifiers) = parse_mouse_cb(cb)?;

    let kind = if final_char == 'm' {
        match kind {
            MouseEventKind::Down(button) => MouseEventKind::Up(button),
            other => other,
        }
    } else {
        kind
    };

    Some(MouseEvent {
        kind,
        column,
        row,
        modifiers,
    })
}

fn parse_mouse_cb(cb: u8) -> Option<(MouseEventKind, KeyModifiers)> {
    let button_number = (cb & 0b0000_0011) | ((cb & 0b1100_0000) >> 4);
    let dragging = cb & 0b0010_0000 == 0b0010_0000;

    let kind = match (button_number, dragging) {
        (0, false) => MouseEventKind::Down(MouseButton::Left),
        (1, false) => MouseEventKind::Down(MouseButton::Middle),
        (2, false) => MouseEventKind::Down(MouseButton::Right),
        (0, true) => MouseEventKind::Drag(MouseButton::Left),
        (1, true) => MouseEventKind::Drag(MouseButton::Middle),
        (2, true) => MouseEventKind::Drag(MouseButton::Right),
        (3, false) => MouseEventKind::Up(MouseButton::Left),
        // Crossterm cannot represent extended-button drags. Preserve their
        // position as motion so a stuck host button cannot suppress hover.
        (3, true) | (4, true) | (5, true) | (8, true) | (9, true) => MouseEventKind::Moved,
        (4, false) => MouseEventKind::ScrollUp,
        (5, false) => MouseEventKind::ScrollDown,
        (6, false) => MouseEventKind::ScrollLeft,
        (7, false) => MouseEventKind::ScrollRight,
        _ => return None,
    };

    let mut modifiers = KeyModifiers::empty();
    if cb & 0b0000_0100 != 0 {
        modifiers |= KeyModifiers::SHIFT;
    }
    if cb & 0b0000_1000 != 0 {
        modifiers |= KeyModifiers::ALT;
    }
    if cb & 0b0001_0000 != 0 {
        modifiers |= KeyModifiers::CONTROL;
    }

    Some((kind, modifiers))
}

#[cfg(test)]
#[path = "raw_input/tests.rs"]
mod tests;
