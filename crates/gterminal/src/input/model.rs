#[cfg(not(windows))]
use crossterm::event::KeyboardEnhancementFlags;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WindowsKeyRecord {
    pub key_down: bool,
    pub repeat_count: u16,
    pub virtual_key_code: u16,
    pub virtual_scan_code: u16,
    pub unicode: u16,
    pub control_key_state: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TextCommit {
    text: String,
}

impl TextCommit {
    pub fn new(text: impl Into<String>) -> Self {
        Self { text: text.into() }
    }

    pub fn as_str(&self) -> &str {
        &self.text
    }

    pub(crate) fn into_string(self) -> String {
        self.text
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct PhysicalKeyId(u32);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum KeyIdentity {
    Physical(PhysicalKeyId),
    Semantic(KeyCode),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum KeySource {
    Synthesized,
    Vt {
        bytes: Vec<u8>,
    },
    WindowsConsole {
        record: WindowsKeyRecord,
        physical_key: Option<PhysicalKeyId>,
    },
}

impl WindowsKeyRecord {
    fn physical_key_id(self) -> Option<PhysicalKeyId> {
        const ENHANCED_KEY: u32 = 0x0100;
        (self.virtual_scan_code != 0).then(|| {
            PhysicalKeyId(
                u32::from(self.virtual_scan_code)
                    | (u32::from(self.control_key_state & ENHANCED_KEY != 0) << 16),
            )
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TerminalKey {
    pub code: KeyCode,
    pub modifiers: KeyModifiers,
    pub kind: crossterm::event::KeyEventKind,
    pub repeat_count: u16,
    pub shifted_codepoint: Option<u32>,
    pub generated_text: Option<String>,
    source: KeySource,
}

impl TerminalKey {
    pub fn new(code: KeyCode, modifiers: KeyModifiers) -> Self {
        Self {
            code,
            modifiers,
            kind: crossterm::event::KeyEventKind::Press,
            repeat_count: 1,
            shifted_codepoint: None,
            generated_text: None,
            source: KeySource::Synthesized,
        }
    }

    pub fn with_kind(mut self, kind: crossterm::event::KeyEventKind) -> Self {
        if kind == crossterm::event::KeyEventKind::Release {
            self.repeat_count = 1;
            self.generated_text = None;
        }
        self.kind = kind;
        self
    }

    pub fn with_repeat_count(mut self, repeat_count: u16) -> Self {
        self.repeat_count = if self.kind == crossterm::event::KeyEventKind::Release {
            1
        } else {
            repeat_count.max(1)
        };
        self
    }

    pub(crate) fn with_modifiers(mut self, modifiers: KeyModifiers) -> Self {
        self.modifiers = modifiers;
        self
    }

    #[allow(dead_code)] // Reserved for the upcoming raw input parser to preserve shifted/base key pairs.
    pub fn with_shifted_codepoint(mut self, shifted_codepoint: u32) -> Self {
        self.shifted_codepoint = Some(shifted_codepoint);
        self
    }

    pub(crate) fn with_generated_text(mut self, text: Option<String>) -> Self {
        self.generated_text = if self.kind == crossterm::event::KeyEventKind::Release {
            None
        } else {
            text
        };
        self
    }

    pub(crate) fn with_vt_bytes(mut self, bytes: Vec<u8>) -> Self {
        self.source = KeySource::Vt { bytes };
        self
    }

    pub fn with_windows_record(mut self, record: WindowsKeyRecord) -> Self {
        self.repeat_count = if self.kind == crossterm::event::KeyEventKind::Release {
            1
        } else {
            record.repeat_count.max(1)
        };
        self.source = KeySource::WindowsConsole {
            physical_key: record.physical_key_id(),
            record,
        };
        self
    }

    #[cfg(any(windows, test))]
    pub(crate) fn vt_bytes(&self) -> Option<&[u8]> {
        match &self.source {
            KeySource::Vt { bytes } => Some(bytes),
            KeySource::Synthesized | KeySource::WindowsConsole { .. } => None,
        }
    }

    #[cfg(any(windows, test))]
    pub(crate) fn windows_record(&self) -> Option<WindowsKeyRecord> {
        match self.source {
            KeySource::WindowsConsole { record, .. } => Some(record),
            KeySource::Synthesized | KeySource::Vt { .. } => None,
        }
    }

    pub(crate) fn identity(&self) -> KeyIdentity {
        match self.source {
            KeySource::WindowsConsole {
                physical_key: Some(physical_key),
                ..
            } => KeyIdentity::Physical(physical_key),
            KeySource::WindowsConsole {
                physical_key: None, ..
            }
            | KeySource::Synthesized
            | KeySource::Vt { .. } => KeyIdentity::Semantic(self.code),
        }
    }

    pub(crate) fn has_physical_identity(&self) -> bool {
        matches!(
            self.source,
            KeySource::WindowsConsole {
                physical_key: Some(_),
                ..
            }
        )
    }

    pub fn with_text_commit(mut self) -> Self {
        let has_text_only_modifiers = match self.code {
            KeyCode::Char(ch) if ch.is_uppercase() => {
                self.modifiers == KeyModifiers::SHIFT || self.modifiers.is_empty()
            }
            KeyCode::Char(_) => self.modifiers.is_empty(),
            _ => false,
        };
        if has_text_only_modifiers && self.kind == crossterm::event::KeyEventKind::Press {
            self.generated_text = match self.code {
                KeyCode::Char(ch) => Some(ch.to_string()),
                _ => None,
            };
        }
        self
    }

    pub fn as_key_event(&self) -> KeyEvent {
        KeyEvent::new_with_kind(self.code, self.modifiers, self.kind)
    }
}

impl From<KeyEvent> for TerminalKey {
    fn from(value: KeyEvent) -> Self {
        Self::new(value.code, value.modifiers).with_kind(value.kind)
    }
}

pub(crate) const KITTY_FLAG_REPORT_ALL_KEYS: u16 = 0b0000_1000;

#[cfg(not(windows))]
pub fn ime_compatible_keyboard_enhancement_flags() -> KeyboardEnhancementFlags {
    KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
        | KeyboardEnhancementFlags::REPORT_EVENT_TYPES
        | KeyboardEnhancementFlags::REPORT_ALTERNATE_KEYS
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModifyOtherKeysMode {
    Mode1,
    Mode2,
}

impl ModifyOtherKeysMode {
    pub fn set_sequence(self) -> &'static [u8] {
        match self {
            Self::Mode1 => b"\x1b[>4;1m",
            Self::Mode2 => b"\x1b[>4;2m",
        }
    }
}

pub fn host_modify_other_keys_mode() -> Option<ModifyOtherKeysMode> {
    #[cfg(windows)]
    let alacritty_window_id = std::env::var_os("ALACRITTY_WINDOW_ID").is_some();
    #[cfg(not(windows))]
    let alacritty_window_id = false;

    host_modify_other_keys_mode_for_env(
        std::env::var("TMUX").is_ok(),
        std::env::var("TERM_PROGRAM").ok().as_deref(),
        std::env::var_os("WEZTERM_PANE").is_some(),
        alacritty_window_id,
    )
}

fn host_modify_other_keys_mode_for_env(
    in_tmux: bool,
    term_program: Option<&str>,
    wezterm_pane: bool,
    alacritty_window_id: bool,
) -> Option<ModifyOtherKeysMode> {
    if in_tmux {
        return Some(ModifyOtherKeysMode::Mode2);
    }

    if wezterm_pane
        || alacritty_window_id
        || term_program.is_some_and(|program| program.eq_ignore_ascii_case("wezterm"))
    {
        return Some(ModifyOtherKeysMode::Mode1);
    }

    None
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyboardProtocol {
    Legacy,
    Kitty { flags: u16 },
}

impl KeyboardProtocol {
    pub fn from_kitty_flags(flags: u16) -> Self {
        if flags == 0 {
            Self::Legacy
        } else {
            Self::Kitty { flags }
        }
    }

    pub(crate) fn reports_event_types(self) -> bool {
        matches!(self, Self::Kitty { flags } if flags & 0b0000_0010 != 0)
    }

    pub(crate) fn reports_all_keys(self) -> bool {
        matches!(self, Self::Kitty { flags } if flags & KITTY_FLAG_REPORT_ALL_KEYS != 0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MouseProtocolMode {
    None,
    Press,
    PressRelease,
    ButtonMotion,
    AnyMotion,
}

impl MouseProtocolMode {
    pub fn reporting_enabled(self) -> bool {
        self != Self::None
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MouseProtocolEncoding {
    Default,
    Utf8,
    Sgr,
}

#[cfg(test)]
#[path = "model/tests.rs"]
mod tests;
