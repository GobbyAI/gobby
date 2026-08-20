use std::borrow::Cow;
use std::path::PathBuf;

use tracing::info;

use crate::layout::PaneId;

use super::terminal::GhosttyPaneCore;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum DefaultColorQuery {
    Foreground,
    Background,
    Cursor,
}

impl DefaultColorQuery {
    pub(super) fn osc_number(self) -> u8 {
        match self {
            Self::Foreground => 10,
            Self::Background => 11,
            Self::Cursor => 12,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum DefaultColorEvent {
    Query(DefaultColorQuery),
    Set(DefaultColorQuery),
    Reset(DefaultColorQuery),
    PaletteQuery(u8),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct DefaultColorTrackedEvent {
    pub(super) end_offset: usize,
    pub(super) event: DefaultColorEvent,
}

#[derive(Debug, Default)]
pub(super) struct DefaultColorOscTracker {
    state: DefaultColorOscTrackerState,
    body: Vec<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum DefaultColorOscTrackerState {
    #[default]
    Ground,
    Escape,
    OscBody,
    OscEscape,
    IgnoreString,
    IgnoreStringEscape,
    OversizedOsc,
    OversizedOscEscape,
}

fn is_ignored_string_intro(byte: u8) -> bool {
    matches!(byte, b'P' | b'_' | b'^' | b'X')
}

impl DefaultColorOscTracker {
    pub(super) fn observe(&mut self, bytes: &[u8]) -> bool {
        let mut saw_default_color_set = false;

        for &byte in bytes {
            match self.state {
                DefaultColorOscTrackerState::Ground => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::Escape;
                    }
                }
                DefaultColorOscTrackerState::Escape => {
                    if byte == b']' {
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::OscBody;
                    } else if is_ignored_string_intro(byte) {
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::IgnoreString;
                    } else if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::Escape;
                    } else {
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                }
                DefaultColorOscTrackerState::OscBody => match byte {
                    0x07 => {
                        saw_default_color_set |= is_default_color_set_osc(&self.body);
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                    0x1b => self.state = DefaultColorOscTrackerState::OscEscape,
                    _ => self.body.push(byte),
                },
                DefaultColorOscTrackerState::OscEscape => {
                    if byte == b'\\' {
                        saw_default_color_set |= is_default_color_set_osc(&self.body);
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else {
                        self.body.push(0x1b);
                        self.body.push(byte);
                        self.state = DefaultColorOscTrackerState::OscBody;
                    }
                }
                DefaultColorOscTrackerState::IgnoreString => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::IgnoreStringEscape;
                    }
                }
                DefaultColorOscTrackerState::IgnoreStringEscape => {
                    if byte == b'\\' {
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = DefaultColorOscTrackerState::IgnoreString;
                    }
                }
                DefaultColorOscTrackerState::OversizedOsc => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::OversizedOscEscape;
                    } else if byte == 0x07 {
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                }
                DefaultColorOscTrackerState::OversizedOscEscape => {
                    if byte == b'\\' {
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = DefaultColorOscTrackerState::OversizedOsc;
                    }
                }
            }

            if self.body.len() > 1024 {
                self.body.clear();
                self.state = DefaultColorOscTrackerState::OversizedOsc;
            }
        }

        saw_default_color_set
    }
}

fn is_default_color_set_osc(body: &[u8]) -> bool {
    parse_default_color_events(body)
        .iter()
        .any(|event| matches!(event, DefaultColorEvent::Set(_)))
}

#[derive(Debug, Default)]
pub(super) struct DefaultColorEventTracker {
    state: DefaultColorOscTrackerState,
    body: Vec<u8>,
    pending: Vec<DefaultColorTrackedEvent>,
}

impl DefaultColorEventTracker {
    pub(super) fn observe(&mut self, bytes: &[u8]) {
        for (index, &byte) in bytes.iter().enumerate() {
            match self.state {
                DefaultColorOscTrackerState::Ground => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::Escape;
                    }
                }
                DefaultColorOscTrackerState::Escape => {
                    if byte == b']' {
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::OscBody;
                    } else if is_ignored_string_intro(byte) {
                        self.body.clear();
                        self.state = DefaultColorOscTrackerState::IgnoreString;
                    } else if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::Escape;
                    } else {
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                }
                DefaultColorOscTrackerState::OscBody => match byte {
                    0x07 => {
                        self.finalize(index + 1);
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                    0x1b => self.state = DefaultColorOscTrackerState::OscEscape,
                    _ => self.body.push(byte),
                },
                DefaultColorOscTrackerState::OscEscape => {
                    if byte == b'\\' {
                        self.finalize(index + 1);
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else {
                        self.body.push(0x1b);
                        self.body.push(byte);
                        self.state = DefaultColorOscTrackerState::OscBody;
                    }
                }
                DefaultColorOscTrackerState::IgnoreString => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::IgnoreStringEscape;
                    }
                }
                DefaultColorOscTrackerState::IgnoreStringEscape => {
                    if byte == b'\\' {
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = DefaultColorOscTrackerState::IgnoreString;
                    }
                }
                DefaultColorOscTrackerState::OversizedOsc => {
                    if byte == 0x1b {
                        self.state = DefaultColorOscTrackerState::OversizedOscEscape;
                    } else if byte == 0x07 {
                        self.state = DefaultColorOscTrackerState::Ground;
                    }
                }
                DefaultColorOscTrackerState::OversizedOscEscape => {
                    if byte == b'\\' {
                        self.state = DefaultColorOscTrackerState::Ground;
                    } else if byte != 0x1b {
                        self.state = DefaultColorOscTrackerState::OversizedOsc;
                    }
                }
            }

            if self.body.len() > 1024 {
                self.body.clear();
                self.state = DefaultColorOscTrackerState::OversizedOsc;
            }
        }
    }

    fn finalize(&mut self, end_offset: usize) {
        self.pending.extend(
            parse_default_color_events(&self.body)
                .into_iter()
                .map(|event| DefaultColorTrackedEvent { end_offset, event }),
        );
        self.body.clear();
    }

    pub(super) fn in_progress_event(&self) -> Option<DefaultColorEvent> {
        if !matches!(
            self.state,
            DefaultColorOscTrackerState::OscBody | DefaultColorOscTrackerState::OscEscape
        ) {
            return None;
        }
        let mut events = parse_default_color_events(&self.body);
        (events.len() == 1).then(|| events.remove(0))
    }

    pub(super) fn drain_pending(&mut self) -> Vec<DefaultColorTrackedEvent> {
        std::mem::take(&mut self.pending)
    }
}

fn parse_default_color_events(body: &[u8]) -> Vec<DefaultColorEvent> {
    let single = match body {
        b"10;?" => Some(DefaultColorEvent::Query(DefaultColorQuery::Foreground)),
        b"11;?" => Some(DefaultColorEvent::Query(DefaultColorQuery::Background)),
        b"12;?" => Some(DefaultColorEvent::Query(DefaultColorQuery::Cursor)),
        b"110" | b"110;" => Some(DefaultColorEvent::Reset(DefaultColorQuery::Foreground)),
        b"111" | b"111;" => Some(DefaultColorEvent::Reset(DefaultColorQuery::Background)),
        _ => parse_palette_color_query(body),
    };
    if let Some(event) = single {
        return vec![event];
    }
    parse_default_color_set_events(body)
}

fn parse_palette_color_query(body: &[u8]) -> Option<DefaultColorEvent> {
    let index = body.strip_prefix(b"4;")?.strip_suffix(b";?")?;
    if index.is_empty() || index.len() > 3 || !index.iter().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let mut value: u16 = 0;
    for &digit in index {
        value = value * 10 + u16::from(digit - b'0');
    }
    u8::try_from(value)
        .ok()
        .map(DefaultColorEvent::PaletteQuery)
}

fn parse_default_color_set_events(body: &[u8]) -> Vec<DefaultColorEvent> {
    let Some(separator) = body.iter().position(|byte| *byte == b';') else {
        return Vec::new();
    };
    let start = match &body[..separator] {
        b"10" => 10,
        b"11" => 11,
        b"12" => 12,
        _ => return Vec::new(),
    };
    body[separator + 1..]
        .split(|byte| *byte == b';')
        .filter(|value| !value.is_empty())
        .enumerate()
        .filter_map(|(offset, value)| {
            if value == b"?" {
                return None;
            }
            let query = match start + offset {
                10 => DefaultColorQuery::Foreground,
                11 => DefaultColorQuery::Background,
                12 => DefaultColorQuery::Cursor,
                _ => return None,
            };
            Some(DefaultColorEvent::Set(query))
        })
        .collect()
}

pub(super) fn parse_reported_cwd(value: &[u8]) -> Option<PathBuf> {
    let value = std::str::from_utf8(value).ok()?.trim();
    if value.starts_with("file://") {
        return parse_file_uri_cwd(value);
    }
    let path = value.trim_matches('"');
    (!path.is_empty()).then(|| PathBuf::from(path))
}

/// Collects complete OSC bodies from a raw byte stream. Consumers receive only
/// bodies, keeping the framing state machine independent from OSC commands.
#[derive(Debug, Default)]
struct OscStreamCollector {
    state: OscStreamState,
    body: Vec<u8>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
enum OscStreamState {
    #[default]
    Ground,
    Escape,
    Body,
    BodyEscape,
    IgnoringString,
    IgnoringStringEscape,
    Discarding,
    DiscardingEscape,
}

impl OscStreamCollector {
    const MAX_BODY_BYTES: usize = 4096;

    fn observe(&mut self, bytes: &[u8], mut receive: impl FnMut(&[u8])) {
        for &byte in bytes {
            match self.state {
                OscStreamState::Ground => {
                    if byte == 0x1b {
                        self.state = OscStreamState::Escape;
                    }
                }
                OscStreamState::Escape => match byte {
                    b']' => {
                        self.body.clear();
                        self.state = OscStreamState::Body;
                    }
                    0x1b => self.state = OscStreamState::Escape,
                    byte if is_ignored_string_intro(byte) => {
                        self.state = OscStreamState::IgnoringString;
                    }
                    _ => self.state = OscStreamState::Ground,
                },
                OscStreamState::Body => match byte {
                    0x07 => self.finish(&mut receive),
                    0x1b => self.state = OscStreamState::BodyEscape,
                    _ => self.push(byte),
                },
                OscStreamState::BodyEscape => match byte {
                    b'\\' => self.finish(&mut receive),
                    0x07 => {
                        self.push(0x1b);
                        if matches!(self.state, OscStreamState::Body) {
                            self.finish(&mut receive);
                        } else {
                            self.state = OscStreamState::Ground;
                        }
                    }
                    0x1b => {
                        self.push(0x1b);
                        self.state = match self.state {
                            OscStreamState::Body => OscStreamState::BodyEscape,
                            OscStreamState::Discarding => OscStreamState::DiscardingEscape,
                            state => state,
                        };
                    }
                    _ => {
                        self.push(0x1b);
                        if matches!(self.state, OscStreamState::Body) {
                            self.push(byte);
                        }
                    }
                },
                OscStreamState::IgnoringString => {
                    if byte == 0x1b {
                        self.state = OscStreamState::IgnoringStringEscape;
                    }
                }
                OscStreamState::IgnoringStringEscape => {
                    if byte == b'\\' {
                        self.state = OscStreamState::Ground;
                    } else if byte != 0x1b {
                        self.state = OscStreamState::IgnoringString;
                    }
                }
                OscStreamState::Discarding => {
                    if byte == 0x07 {
                        self.state = OscStreamState::Ground;
                    } else if byte == 0x1b {
                        self.state = OscStreamState::DiscardingEscape;
                    }
                }
                OscStreamState::DiscardingEscape => {
                    if byte == b'\\' {
                        self.state = OscStreamState::Ground;
                    } else if byte != 0x1b {
                        self.state = OscStreamState::Discarding;
                    }
                }
            }
        }
    }

    fn push(&mut self, byte: u8) {
        self.body.push(byte);
        if self.body.len() > Self::MAX_BODY_BYTES {
            self.body.clear();
            self.state = OscStreamState::Discarding;
        } else {
            self.state = OscStreamState::Body;
        }
    }

    fn finish(&mut self, receive: &mut impl FnMut(&[u8])) {
        receive(&self.body);
        self.body.clear();
        self.state = OscStreamState::Ground;
    }
}

/// Maximum retained string length for agent OSC title and progress payloads.
/// Title text is untrusted model output; cap it to bound memory and log size.
const AGENT_OSC_MAX_CHARS: usize = 256;

/// Always-on tracker that retains the latest OSC 0/2 title and OSC 9 progress
/// payload emitted by the child process. Nothing here affects rendering; this
/// is pure passive capture for the detection engine (Stage C / Stage D).
///
/// - `latest_title` — last OSC 0 or OSC 2 payload, sanitized. An empty
///   payload (e.g. `\x1b]0;\x07`) clears the stored value.
/// - `latest_progress` — last OSC 9 payload (the part after `9;`), stored
///   as-is after sanitization. E.g. `"4;3;"` or `"4;0;"`.
#[derive(Debug, Default)]
pub(super) struct AgentOscStateTracker {
    collector: OscStreamCollector,
    latest_title: Option<String>,
    terminal_title: Option<String>,
    latest_progress: Option<String>,
}

impl AgentOscStateTracker {
    pub(super) fn observe(&mut self, bytes: &[u8]) {
        let (collector, latest_title, terminal_title, latest_progress) = (
            &mut self.collector,
            &mut self.latest_title,
            &mut self.terminal_title,
            &mut self.latest_progress,
        );
        collector.observe(bytes, |body| {
            let Some((command, payload)) = parse_agent_osc_body(body) else {
                return;
            };
            match command {
                b"0" | b"2" => {
                    let title = sanitize_agent_osc_string(payload, AGENT_OSC_MAX_CHARS);
                    *terminal_title = (!title.is_empty()).then_some(title.clone());
                    *latest_title = (!title.is_empty()).then_some(title);
                }
                b"9" => {
                    *latest_progress =
                        Some(sanitize_agent_osc_string(payload, AGENT_OSC_MAX_CHARS));
                }
                _ => {}
            }
        });
    }

    pub(super) fn terminal_title(&self) -> Option<&str> {
        self.terminal_title.as_deref()
    }

    #[cfg(unix)]
    pub(super) fn seed_terminal_title(&mut self, title: Option<String>) {
        self.terminal_title = title;
    }

    /// Returns the latest retained OSC title, or `""` if none has been seen or
    /// the last title was an empty clear.
    #[allow(dead_code)] // used by terminal.rs; full call chain wired in Stage C
    pub(super) fn latest_title(&self) -> &str {
        self.latest_title.as_deref().unwrap_or("")
    }

    /// Returns the latest retained OSC 9 progress payload, or `""` if none.
    #[allow(dead_code)] // used by terminal.rs; full call chain wired in Stage C
    pub(super) fn latest_progress(&self) -> &str {
        self.latest_progress.as_deref().unwrap_or("")
    }

    /// Drops the retained title and progress so a new foreground agent cannot
    /// inherit OSC evidence emitted by a previous process. The in-flight parse
    /// state is kept: a sequence spanning the agent change finalizes normally
    /// and is attributed to the new agent.
    pub(super) fn clear_retained(&mut self) {
        self.latest_title = None;
        self.latest_progress = None;
    }
}

/// Splits an OSC body at the first `;`, returning `(command, payload)`.
/// Returns `None` if there is no `;`.
fn parse_agent_osc_body(body: &[u8]) -> Option<(&[u8], &[u8])> {
    let sep = body.iter().position(|&b| b == b';')?;
    Some((&body[..sep], &body[sep + 1..]))
}

fn sanitize_agent_osc_string(payload: &[u8], max_chars: usize) -> String {
    let text = String::from_utf8_lossy(payload);
    let mut out = String::new();
    for ch in text.chars().filter(|ch| !ch.is_control()).take(max_chars) {
        out.push(ch);
    }
    out
}

/// Reconstructs selected OSC sequences for local evidence capture while
/// debugging agent title/status behavior. This is intentionally passive:
/// nothing here affects terminal rendering or detection state.
#[derive(Debug)]
pub(super) struct OscDebugTracker {
    enabled: bool,
    collector: OscStreamCollector,
    pending: Vec<OscDebugEvent>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct OscDebugEvent {
    pub(super) command: String,
    pub(super) payload: String,
}

impl OscDebugTracker {
    pub(super) fn from_env() -> Self {
        Self {
            enabled: osc_debug_enabled_from_env(),
            collector: OscStreamCollector::default(),
            pending: Vec::new(),
        }
    }

    pub(super) fn observe(&mut self, bytes: &[u8]) {
        if !self.enabled {
            return;
        }
        let (collector, pending) = (&mut self.collector, &mut self.pending);
        collector.observe(bytes, |body| {
            if let Some(event) = parse_osc_debug_event(body) {
                pending.push(event);
            }
        });
    }

    pub(super) fn drain_pending(&mut self) -> Vec<OscDebugEvent> {
        std::mem::take(&mut self.pending)
    }
}

impl Default for OscDebugTracker {
    fn default() -> Self {
        Self::from_env()
    }
}

fn osc_debug_enabled_from_env() -> bool {
    std::env::var("GTERM_DEBUG_OSC_EVIDENCE")
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn parse_osc_debug_event(body: &[u8]) -> Option<OscDebugEvent> {
    let separator = body.iter().position(|byte| *byte == b';')?;
    let command = &body[..separator];
    let payload = &body[separator + 1..];
    if !matches!(command, b"0" | b"2" | b"9" | b"21337") {
        return None;
    }
    Some(OscDebugEvent {
        command: std::str::from_utf8(command).ok()?.to_string(),
        payload: sanitized_osc_debug_payload(payload),
    })
}

fn sanitized_osc_debug_payload(payload: &[u8]) -> String {
    const MAX_CHARS: usize = 512;
    let text = String::from_utf8_lossy(payload);
    let mut sanitized = String::new();
    for ch in text.chars().filter(|ch| !ch.is_control()).take(MAX_CHARS) {
        sanitized.push(ch);
    }
    if text.chars().count() > MAX_CHARS {
        sanitized.push_str("...");
    }
    sanitized
}

fn parse_file_uri_cwd(uri: &str) -> Option<PathBuf> {
    let rest = uri.strip_prefix("file://")?;
    let path = if rest.starts_with('/') {
        rest
    } else if let Some(slash) = rest.find('/') {
        let host = &rest[..slash];
        if !(host.is_empty() || host.eq_ignore_ascii_case("localhost")) {
            return None;
        }
        &rest[slash..]
    } else {
        rest
    };
    let path = percent_decode_utf8(path)?;

    #[cfg(windows)]
    {
        let mut path = path;
        if path.len() >= 3
            && path.as_bytes()[0] == b'/'
            && path.as_bytes()[2] == b':'
            && path.as_bytes()[1].is_ascii_alphabetic()
        {
            path.remove(0);
        }
        Some(PathBuf::from(path.replace('/', "\\")))
    }

    #[cfg(not(windows))]
    Some(PathBuf::from(path))
}

fn percent_decode_utf8(input: &str) -> Option<String> {
    let bytes = input.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut idx = 0;
    while idx < bytes.len() {
        if bytes[idx] == b'%' {
            let hi = *bytes.get(idx + 1)?;
            let lo = *bytes.get(idx + 2)?;
            output.push(hex_value(hi)? * 16 + hex_value(lo)?);
            idx += 3;
        } else {
            output.push(bytes[idx]);
            idx += 1;
        }
    }
    String::from_utf8(output).ok()
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn foreground_job_is_shell(job: &crate::platform::ForegroundJob, shell_pid: u32) -> bool {
    job.processes.iter().any(|process| process.pid == shell_pid)
}

pub(super) fn current_transient_default_color_owner(shell_pid: u32) -> Option<u32> {
    let job = crate::platform::foreground_job(shell_pid)?;
    (!foreground_job_is_shell(&job, shell_pid)).then_some(job.process_group_id)
}

fn foreground_job_uses_droid_scrollback_compat(job: &crate::platform::ForegroundJob) -> bool {
    job.processes.iter().any(|process| {
        process.name.eq_ignore_ascii_case("droid")
            || process
                .argv0
                .as_deref()
                .is_some_and(|argv0| argv0.eq_ignore_ascii_case("droid"))
            || process.cmdline.as_deref().is_some_and(|cmdline| {
                cmdline.eq_ignore_ascii_case("droid")
                    || cmdline.starts_with("droid ")
                    || cmdline.to_ascii_lowercase().contains("/droid")
            })
    })
}

pub(super) fn contains_scrollback_clear_sequence(bytes: &[u8]) -> bool {
    bytes.windows(4).any(|window| window == b"\x1b[3J")
        || bytes.windows(5).any(|window| window == b"\x1b[?3J")
}

fn strip_scrollback_clear_sequences<'a>(bytes: &'a [u8]) -> Cow<'a, [u8]> {
    if !contains_scrollback_clear_sequence(bytes) {
        return Cow::Borrowed(bytes);
    }

    let mut filtered = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        let remaining = &bytes[index..];
        if remaining.starts_with(b"\x1b[3J") {
            index += 4;
            continue;
        }
        if remaining.starts_with(b"\x1b[?3J") {
            index += 5;
            continue;
        }
        filtered.push(bytes[index]);
        index += 1;
    }

    Cow::Owned(filtered)
}

pub(super) fn maybe_filter_primary_screen_scrollback_clear<'a>(
    bytes: &'a [u8],
    alternate_screen: bool,
    foreground_job: Option<&crate::platform::ForegroundJob>,
) -> Cow<'a, [u8]> {
    // Droid redraws its primary-screen TUI with CSI 3 J, which erases pane
    // scrollback inside gterm. Keep the hack scoped to Droid on the primary
    // screen so normal terminal clear-history behavior still works elsewhere.
    if alternate_screen
        || !contains_scrollback_clear_sequence(bytes)
        || !foreground_job.is_some_and(foreground_job_uses_droid_scrollback_compat)
    {
        return Cow::Borrowed(bytes);
    }

    strip_scrollback_clear_sequences(bytes)
}

#[cfg(target_os = "macos")]
pub(super) fn should_restore_host_terminal_theme(
    owner_pgid: u32,
    shell_pid: u32,
    alternate_screen: bool,
    foreground_job: Option<&crate::platform::ForegroundJob>,
) -> bool {
    if alternate_screen {
        return false;
    }

    let Some(foreground_job) = foreground_job else {
        return false;
    };

    let _ = owner_pgid;
    foreground_job_is_shell(foreground_job, shell_pid)
}

#[cfg(not(target_os = "macos"))]
pub(super) fn should_restore_host_terminal_theme(
    owner_pgid: u32,
    shell_pid: u32,
    alternate_screen: bool,
    foreground_job: Option<&crate::platform::ForegroundJob>,
) -> bool {
    if alternate_screen {
        return false;
    }

    let Some(foreground_job) = foreground_job else {
        return false;
    };

    foreground_job.process_group_id != owner_pgid
        && foreground_job_is_shell(foreground_job, shell_pid)
}

pub(super) fn write_host_terminal_theme(
    terminal: &mut crate::ghostty::Terminal,
    theme: crate::terminal_theme::TerminalTheme,
) {
    write_host_terminal_theme_selective(terminal, theme, true, true);
}

pub(super) fn write_host_terminal_theme_selective(
    terminal: &mut crate::ghostty::Terminal,
    theme: crate::terminal_theme::TerminalTheme,
    foreground: bool,
    background: bool,
) {
    if foreground {
        write_host_default_color(
            terminal,
            crate::terminal_theme::DefaultColorKind::Foreground,
            theme.foreground,
        );
    }
    if background {
        write_host_default_color(
            terminal,
            crate::terminal_theme::DefaultColorKind::Background,
            theme.background,
        );
    }
}

fn write_host_default_color(
    terminal: &mut crate::ghostty::Terminal,
    kind: crate::terminal_theme::DefaultColorKind,
    color: Option<crate::terminal_theme::RgbColor>,
) {
    let sequence = if let Some(color) = color {
        crate::terminal_theme::osc_set_default_color_sequence(kind, color)
    } else {
        crate::terminal_theme::osc_reset_default_color_sequence(kind).to_string()
    };
    terminal.write(sequence.as_bytes());
}

pub(super) fn restore_host_terminal_theme_if_needed(
    core: &mut GhosttyPaneCore,
    pane_id: PaneId,
    shell_pid: u32,
    alternate_screen: bool,
    foreground_job: Option<&crate::platform::ForegroundJob>,
) -> bool {
    let Some(owner_pgid) = core.transient_default_color_owner_pgid else {
        return false;
    };
    if core.host_terminal_theme.is_empty() {
        return false;
    }
    if !should_restore_host_terminal_theme(owner_pgid, shell_pid, alternate_screen, foreground_job)
    {
        return false;
    }

    core.transient_default_color_owner_pgid = None;
    core.child_default_foreground_changed = false;
    core.child_default_background_changed = false;
    write_host_terminal_theme(&mut core.terminal, core.host_terminal_theme);
    info!(
        pane = pane_id.raw(),
        owner_pgid, "restored host terminal default colors after transient override"
    );
    true
}

#[cfg(test)]
#[path = "osc/tests.rs"]
mod tests;
