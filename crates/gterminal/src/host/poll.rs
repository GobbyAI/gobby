//! Parse tmux capture-poll batches and classify invocation outcomes.

use std::io::ErrorKind;

use crate::protocol::{
    CellData, CursorState, FrameData, ObservationReason, PaneModes, MAX_CELLS, MAX_COLS, MAX_ROWS,
};

pub const POLL_FIELD_COUNT: usize = 28;
pub const STDOUT_CAP: usize = 2 * 1024 * 1024 + 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PollClass {
    Live,
    ConfirmedAbsence,
    SpawnFailed,
    Timeout,
    Permission,
    FdExhausted,
    Unparseable,
    GeometryOversize,
}

impl PollClass {
    pub fn reason(self) -> Option<ObservationReason> {
        match self {
            Self::Live | Self::ConfirmedAbsence => None,
            Self::SpawnFailed => Some(ObservationReason::PollSpawnFailed),
            Self::Timeout => Some(ObservationReason::PollTimeout),
            Self::Permission => Some(ObservationReason::PollPermission),
            Self::FdExhausted => Some(ObservationReason::PollFdExhausted),
            Self::Unparseable => Some(ObservationReason::PollUnparseable),
            Self::GeometryOversize => Some(ObservationReason::GeometryExceedsMaxCells),
        }
    }
}

#[derive(Debug, Clone)]
pub struct ParsedPoll {
    pub pid: i32,
    pub start_time: i64,
    pub width: u16,
    pub height: u16,
    pub cursor_x: u16,
    pub cursor_y: u16,
    pub pane_dead: bool,
    pub title: String,
    pub capture: String,
    pub modes: PaneModes,
}

pub fn parse_poll_batch(batch: &str) -> Option<ParsedPoll> {
    let header_end = batch.find('\n')?;
    let header = &batch[..header_end];
    let rest = &batch[header_end + 1..];
    let fields: Vec<&str> = header.split_whitespace().collect();
    if fields.len() < POLL_FIELD_COUNT {
        return None;
    }
    let pid: i32 = fields[0].parse().ok()?;
    let start_time: i64 = fields[1].parse().ok()?;
    let width: u16 = fields[2].parse().ok()?;
    let height: u16 = fields[3].parse().ok()?;
    let cursor_x: u16 = fields[4].parse().ok()?;
    let cursor_y: u16 = fields[5].parse().ok()?;
    let flag = |i: usize| fields[i] == "1";
    let modes = PaneModes {
        cursor_visible: flag(6),
        cursor_very_visible: flag(7),
        alternate_on: flag(8),
        keypad_cursor: flag(9),
        keypad: flag(10),
        bracket_paste: flag(11),
        mouse_standard: flag(12),
        mouse_button: flag(13),
        mouse_any: flag(14),
        mouse_all: flag(15),
        mouse_sgr: flag(16),
        wrap: flag(17),
        origin: flag(18),
        insert: flag(19),
        scroll_region_upper: fields[20].parse().unwrap_or(0),
        scroll_region_lower: fields[21].parse().unwrap_or(0),
        pane_in_mode: flag(22),
        cursor_shape: fields[23].parse().unwrap_or(0),
        cursor_blinking: flag(24),
        cursor_colour: fields[25].to_string(),
        mouse_utf8: flag(26),
    };
    let pane_dead = fields[27] == "1";
    let title_len_line = rest.strip_prefix("GTERM_TITLE_LEN=")?;
    let (len_str, after_len) = title_len_line.split_once('\n')?;
    let title_len: usize = len_str.parse().ok()?;
    let title_line = after_len.strip_prefix("GTERM_TITLE=")?;
    if title_line.len() < title_len {
        return None;
    }
    let title = title_line[..title_len].to_string();
    let mut capture = &title_line[title_len..];
    if let Some(stripped) = capture.strip_prefix('\n') {
        capture = stripped;
    }
    Some(ParsedPoll {
        pid,
        start_time,
        width,
        height,
        cursor_x,
        cursor_y,
        pane_dead,
        title,
        capture: capture.to_string(),
        modes,
    })
}

pub fn classify_poll(
    exit: Option<i32>,
    stderr: &str,
    stdout: &str,
    io_kind: Option<ErrorKind>,
    timed_out: Option<bool>,
) -> PollClass {
    if timed_out == Some(true) {
        return PollClass::Timeout;
    }
    let err = stderr.to_ascii_lowercase();
    if matches!(
        io_kind,
        Some(ErrorKind::NotFound | ErrorKind::OutOfMemory | ErrorKind::Other)
    ) && stdout.is_empty()
        && (err.contains("fork") || io_kind == Some(ErrorKind::OutOfMemory))
    {
        return PollClass::SpawnFailed;
    }
    if err.contains("permission denied") {
        return PollClass::Permission;
    }
    if err.contains("too many open files") || err.contains("emfile") {
        return PollClass::FdExhausted;
    }
    if err.contains("can't find pane")
        || err.contains("no pane")
        || err.contains("no server running")
        || err.contains("error connecting to")
        || err.contains("no current target")
    {
        return PollClass::ConfirmedAbsence;
    }
    if let Some(code) = exit {
        if code != 0 {
            if err.contains("fork") {
                return PollClass::SpawnFailed;
            }
            if err.contains("can't find") || err.contains("no server") {
                return PollClass::ConfirmedAbsence;
            }
            return PollClass::Unparseable;
        }
    }
    if parse_poll_batch(stdout).is_none() {
        return PollClass::Unparseable;
    }
    if let Some(parsed) = parse_poll_batch(stdout) {
        if parsed.pane_dead {
            return PollClass::ConfirmedAbsence;
        }
        if geometry_oversize(parsed.width, parsed.height) {
            return PollClass::GeometryOversize;
        }
        return PollClass::Live;
    }
    PollClass::Unparseable
}

pub fn geometry_oversize(width: u16, height: u16) -> bool {
    width == 0
        || height == 0
        || width > MAX_COLS
        || height > MAX_ROWS
        || u64::from(width) * u64::from(height) > MAX_CELLS as u64
}

pub fn truncate_attach_history(
    text: &str,
    max_bytes: usize,
    max_lines: usize,
) -> (String, bool, u64, u64) {
    let total = text.len() as u64;
    let mut lines: Vec<&str> = text.split('\n').collect();
    let mut truncated = false;
    if lines.len() > max_lines {
        truncated = true;
        lines = lines[lines.len() - max_lines..].to_vec();
    }
    let mut joined = lines.join("\n");
    if joined.len() > max_bytes {
        truncated = true;
        let mut start = joined.len() - max_bytes;
        while start < joined.len() && !joined.is_char_boundary(start) {
            start += 1;
        }
        joined = joined[start..].to_string();
    }
    let dropped = total.saturating_sub(joined.len() as u64);
    (joined, truncated, dropped, total)
}

pub fn capture_to_frame(
    text: &str,
    width: u16,
    height: u16,
    modes: PaneModes,
    cursor: CursorState,
) -> FrameData {
    let width = width.max(1);
    let height = height.max(1);
    let mut cells = vec![
        CellData {
            symbol: " ".into(),
            fg: 0,
            bg: 0,
            modifier: 0,
            skip: false,
            hyperlink: None,
        };
        width as usize * height as usize
    ];
    let mut x = 0u16;
    let mut y = 0u16;
    let mut fg = 0u32;
    let mut bg = 0u32;
    let mut modifier = 0u16;
    let mut chars = text.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\u{1b}' {
            if chars.peek() == Some(&'[') {
                chars.next();
                let mut params = String::new();
                for next in chars.by_ref() {
                    if next.is_ascii_alphabetic() {
                        if next == 'm' {
                            apply_sgr(&params, &mut fg, &mut bg, &mut modifier);
                        }
                        break;
                    }
                    params.push(next);
                }
            }
            continue;
        }
        if ch == '\n' {
            x = 0;
            y = y.saturating_add(1);
            continue;
        }
        if ch == '\r' {
            x = 0;
            continue;
        }
        if y >= height {
            break;
        }
        if x < width {
            let idx = y as usize * width as usize + x as usize;
            cells[idx] = CellData {
                symbol: ch.to_string(),
                fg,
                bg,
                modifier,
                skip: false,
                hyperlink: None,
            };
            x = x.saturating_add(1);
        }
    }
    FrameData {
        cells,
        width,
        height,
        cursor: Some(cursor),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes,
    }
}

fn apply_sgr(params: &str, fg: &mut u32, bg: &mut u32, modifier: &mut u16) {
    if params.is_empty() {
        *fg = 0;
        *bg = 0;
        *modifier = 0;
        return;
    }
    let parts: Vec<u32> = params.split(';').filter_map(|p| p.parse().ok()).collect();
    let mut i = 0;
    while i < parts.len() {
        match parts[i] {
            0 => {
                *fg = 0;
                *bg = 0;
                *modifier = 0;
            }
            1 => *modifier |= 1,
            22 => *modifier &= !1,
            30..=37 => *fg = parts[i] - 29,
            40..=47 => *bg = parts[i] - 39,
            39 => *fg = 0,
            49 => *bg = 0,
            38 if i + 2 < parts.len() && parts[i + 1] == 5 => {
                *fg = 0x01_00_00_00 | parts[i + 2];
                i += 2;
            }
            48 if i + 2 < parts.len() && parts[i + 1] == 5 => {
                *bg = 0x01_00_00_00 | parts[i + 2];
                i += 2;
            }
            38 if i + 4 < parts.len() && parts[i + 1] == 2 => {
                *fg = 0x02_00_00_00 | (parts[i + 2] << 16) | (parts[i + 3] << 8) | parts[i + 4];
                i += 4;
            }
            48 if i + 4 < parts.len() && parts[i + 1] == 2 => {
                *bg = 0x02_00_00_00 | (parts[i + 2] << 16) | (parts[i + 3] << 8) | parts[i + 4];
                i += 4;
            }
            _ => {}
        }
        i += 1;
    }
}

pub fn numeric_format() -> &'static str {
    "#{pid} #{start_time} #{pane_width} #{pane_height} #{cursor_x} #{cursor_y} #{cursor_flag} #{cursor_very_visible} #{alternate_on} #{keypad_cursor_flag} #{keypad_flag} #{bracket_paste_flag} #{mouse_standard_flag} #{mouse_button_flag} #{mouse_any_flag} #{mouse_all_flag} #{mouse_sgr_flag} #{wrap_flag} #{origin_flag} #{insert_flag} #{scroll_region_upper} #{scroll_region_lower} #{pane_in_mode} #{cursor_shape} #{cursor_blinking} #{cursor_colour} #{mouse_utf8_flag} #{pane_dead}"
}
