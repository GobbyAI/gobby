// upstream: herdr v0.8.0 src/config/keybinds.rs
//! Keymap: herdr's v0.8.0 default bindings for the keep-set, Gobby's
//! control bindings, and client-local overrides from
//! `~/.gobby/client/keymap.toml`, merged by action name and rejected on any
//! chord collision.
//!
//! Chord grammar (herdr): `prefix+<key>` binds inside prefix mode, anything
//! else binds directly; modifiers `ctrl`, `alt`, `shift`, `super`, `hyper`,
//! `meta`; names `space`, `enter`, `esc`, `tab`, `backspace`, `left`,
//! `right`, `up`, `down`, `minus`, `f1`..`f12`; indexed actions take a
//! `1..9` range. No TOML is parsed by hand: overrides go through `toml`.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use thiserror::Error;

pub type KeyCombo = (KeyCode, KeyModifiers);

pub const DEFAULT_PREFIX: &str = "ctrl+b";

/// Where client-local overrides live.
pub fn default_override_path() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".gobby/client/keymap.toml"))
}

mod names;

pub use names::{Action, BindingSpec, BINDINGS};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Trigger {
    Direct(KeyCombo),
    Prefix(KeyCombo),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chord {
    pub trigger: Trigger,
    pub action: Action,
    /// herdr label (`prefix+shift+n`), for help and diagnostics.
    pub label: String,
}

#[derive(Debug, Clone)]
pub struct Binding {
    pub name: &'static str,
    pub description: &'static str,
    pub reserved: bool,
    pub chords: Vec<Chord>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HelpEntry {
    pub name: &'static str,
    pub description: &'static str,
    /// Comma-joined chord labels.
    pub keys: String,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum KeymapError {
    #[error("keymap override is not valid TOML: {0}")]
    Parse(String),
    #[error("keymap override names unknown action {0:?}")]
    UnknownAction(String),
    #[error("keymap override names reserved action {0:?}; it cannot be activated")]
    ReservedAction(String),
    #[error("keymap override for {action:?} has invalid chord {chord:?}")]
    InvalidChord { action: String, chord: String },
    #[error(
        "keymap override binds {chord:?} to {action:?}, but {displaced:?} already owns it; \
         rebind one of them"
    )]
    Collision {
        chord: String,
        action: String,
        displaced: String,
    },
}

/// `~/.gobby/client/keymap.toml`: an optional `prefix` and a `[bindings]`
/// table of `name = "chord"` or `name = ["chord", ...]`.
#[derive(Debug, Deserialize)]
struct OverrideFile {
    prefix: Option<String>,
    #[serde(default)]
    bindings: BTreeMap<String, ChordList>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum ChordList {
    One(String),
    Many(Vec<String>),
}

impl ChordList {
    fn values(&self) -> Vec<&str> {
        match self {
            ChordList::One(value) => vec![value.as_str()],
            ChordList::Many(values) => values.iter().map(String::as_str).collect(),
        }
    }
}

type Overrides<'a> = BTreeMap<&'a str, Vec<&'a str>>;

/// Active bindings: one action per chord.
#[derive(Debug, Clone)]
pub struct Keymap {
    pub prefix: KeyCombo,
    pub prefix_label: String,
    pub bindings: Vec<Binding>,
}

impl Keymap {
    /// herdr v0.8.0 defaults for the keep-set plus Gobby's control bindings.
    pub fn defaults() -> Self {
        // The static table is validated by tests/keymap.rs; a bad default
        // chord is a build defect, not a runtime condition.
        Self::build(DEFAULT_PREFIX, &Overrides::new())
            .expect("BINDINGS holds only parseable, collision-free chords")
    }

    /// Parse an override document and merge it by action name over the
    /// defaults. Any error leaves the caller on the unmodified defaults.
    pub fn from_toml(text: &str) -> Result<Self, KeymapError> {
        let file: OverrideFile =
            toml::from_str(text).map_err(|err| KeymapError::Parse(err.to_string()))?;
        let overrides: Overrides<'_> = file
            .bindings
            .iter()
            .map(|(name, chords)| (name.as_str(), chords.values()))
            .collect();
        Self::build(file.prefix.as_deref().unwrap_or(DEFAULT_PREFIX), &overrides)
    }

    /// Load overrides from `path`; a missing file yields the defaults.
    pub fn load_overrides(path: &Path) -> Result<Self, KeymapError> {
        match fs::read_to_string(path) {
            Ok(text) => Self::from_toml(&text),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Self::defaults()),
            Err(err) => Err(KeymapError::Parse(err.to_string())),
        }
    }

    fn build(prefix: &str, overrides: &Overrides<'_>) -> Result<Self, KeymapError> {
        let prefix_combo = parse_key_combo(prefix).ok_or_else(|| KeymapError::InvalidChord {
            action: "prefix".to_string(),
            chord: prefix.to_string(),
        })?;
        for name in overrides.keys() {
            match BINDINGS.iter().find(|spec| spec.name == *name) {
                None => return Err(KeymapError::UnknownAction(name.to_string())),
                Some(spec) if spec.reserved => {
                    return Err(KeymapError::ReservedAction(name.to_string()))
                }
                Some(_) => {}
            }
        }
        let mut bindings = Vec::with_capacity(BINDINGS.len());
        for spec in BINDINGS {
            let raws = match overrides.get(spec.name) {
                Some(raws) => raws.clone(),
                None => spec.defaults.to_vec(),
            };
            bindings.push(Binding {
                name: spec.name,
                description: spec.description,
                reserved: spec.reserved,
                chords: expand_chords(spec, &raws)?,
            });
        }
        let keymap = Self {
            prefix: prefix_combo,
            prefix_label: format_key_combo(prefix_combo),
            bindings,
        };
        keymap.check_collisions(overrides)?;
        Ok(keymap)
    }

    /// A chord owned by two non-reserved actions is a collision; the
    /// overriding action is reported first, the default it displaced second.
    fn check_collisions(&self, overrides: &Overrides<'_>) -> Result<(), KeymapError> {
        let mut owners: HashMap<Trigger, &'static str> = HashMap::new();
        for binding in self.bindings.iter().filter(|b| !b.reserved) {
            for chord in &binding.chords {
                let Some(other) = owners.insert(chord.trigger, binding.name) else {
                    continue;
                };
                let overriding = overrides.contains_key(binding.name);
                let (action, displaced) = if overriding || !overrides.contains_key(other) {
                    (binding.name, other)
                } else {
                    (other, binding.name)
                };
                return Err(KeymapError::Collision {
                    chord: chord.label.clone(),
                    action: action.to_string(),
                    displaced: displaced.to_string(),
                });
            }
        }
        Ok(())
    }

    pub fn binding(&self, name: &str) -> Option<&Binding> {
        self.bindings.iter().find(|b| b.name == name)
    }

    pub fn is_prefix(&self, key: &KeyEvent) -> bool {
        key_event_matches_combo(key, self.prefix)
    }

    /// Action bound directly (outside prefix mode), reserved excluded.
    pub fn lookup_direct(&self, key: &KeyEvent) -> Option<Action> {
        self.active()
            .find(|(trigger, _)| {
                matches!(trigger, Trigger::Direct(combo) if key_event_matches_combo(key, *combo))
            })
            .map(|(_, action)| action)
    }

    /// Action bound inside prefix mode, reserved excluded.
    pub fn lookup_prefix(&self, key: &KeyEvent) -> Option<Action> {
        self.active()
            .find(|(trigger, _)| {
                matches!(trigger, Trigger::Prefix(combo) if key_event_matches_combo(key, *combo))
            })
            .map(|(_, action)| action)
    }

    /// Every active (non-reserved) chord with its owner; each chord appears once.
    pub fn active_chords(&self) -> Vec<(Trigger, Action)> {
        self.active().collect()
    }

    /// Help rows in table order, reserved actions hidden.
    pub fn help_entries(&self) -> Vec<HelpEntry> {
        self.bindings
            .iter()
            .filter(|b| !b.reserved)
            .map(|b| HelpEntry {
                name: b.name,
                description: b.description,
                keys: keys_label(&b.chords),
            })
            .collect()
    }

    fn active(&self) -> impl Iterator<Item = (Trigger, Action)> + '_ {
        self.bindings
            .iter()
            .filter(|b| !b.reserved)
            .flat_map(|b| b.chords.iter().map(|c| (c.trigger, c.action)))
    }
}

/// One parsed chord string: a single trigger, or a `1..9` range.
enum ParsedChord {
    Single(Trigger, String),
    Range(Vec<(Trigger, String)>),
}

/// herdr `parse_binding_string`: `prefix+` selects prefix mode; a `1..9`
/// body expands to nine digit chords.
fn parse_binding_string(raw: &str) -> Option<ParsedChord> {
    let trimmed = raw.trim();
    let (trigger_prefix, body) = match trimmed.strip_prefix("prefix+") {
        Some(rest) => (true, rest),
        None => (false, trimmed),
    };
    let make = |combo: KeyCombo| {
        let key_label = format_key_combo(combo);
        if trigger_prefix {
            (Trigger::Prefix(combo), format!("prefix+{key_label}"))
        } else {
            (Trigger::Direct(combo), key_label)
        }
    };

    if let Some(range_modifiers) = parse_range_modifiers(body) {
        let chords = (1..=9)
            .map(|idx| {
                make((
                    KeyCode::Char(char::from_digit(idx, 10).unwrap_or('1')),
                    range_modifiers,
                ))
            })
            .collect();
        return Some(ParsedChord::Range(chords));
    }

    let (trigger, label) = make(parse_key_combo(body)?);
    Some(ParsedChord::Single(trigger, label))
}

/// Expand a binding's chord strings; indexed actions take a range or one
/// chord per index in order.
fn expand_chords(spec: &BindingSpec, raws: &[&str]) -> Result<Vec<Chord>, KeymapError> {
    let mut chords = Vec::new();
    let mut next_index: u8 = 1;
    for raw in raws {
        let invalid = || KeymapError::InvalidChord {
            action: spec.name.to_string(),
            chord: raw.to_string(),
        };
        let parsed = parse_binding_string(raw).ok_or_else(invalid)?;
        match (parsed, spec.indexed) {
            (ParsedChord::Range(entries), true) => {
                for (index, (trigger, label)) in (1..=9u8).zip(entries) {
                    let action = Action::from_name(spec.name, Some(index)).ok_or_else(invalid)?;
                    chords.push(Chord {
                        trigger,
                        action,
                        label,
                    });
                }
                next_index = 10;
            }
            (ParsedChord::Single(trigger, label), true) => {
                let action = Action::from_name(spec.name, Some(next_index)).ok_or_else(invalid)?;
                next_index = next_index.saturating_add(1);
                chords.push(Chord {
                    trigger,
                    action,
                    label,
                });
            }
            (ParsedChord::Single(trigger, label), false) => {
                let action = Action::from_name(spec.name, None).ok_or_else(invalid)?;
                chords.push(Chord {
                    trigger,
                    action,
                    label,
                });
            }
            (ParsedChord::Range(_), false) => return Err(invalid()),
        }
    }
    Ok(chords)
}

/// herdr `indexed_label`: nine consecutive digit chords collapse to `1..9`.
fn keys_label(chords: &[Chord]) -> String {
    if chords.is_empty() {
        return "unset".to_string();
    }
    let labels: Vec<&str> = chords.iter().map(|c| c.label.as_str()).collect();
    let mut parts = Vec::new();
    let mut index = 0;
    while index < labels.len() {
        if let Some(prefix) = indexed_range_prefix(&labels[index..]) {
            parts.push(format!("{prefix}1..9"));
            index += 9;
        } else {
            parts.push(labels[index].to_string());
            index += 1;
        }
    }
    parts.join(", ")
}

fn indexed_range_prefix<'a>(labels: &[&'a str]) -> Option<&'a str> {
    let run = labels.get(..9)?;
    let prefix = run[0].strip_suffix('1')?;
    for (offset, label) in run.iter().enumerate() {
        let digit = char::from(b'1' + offset as u8);
        if label.strip_suffix(digit) != Some(prefix) {
            return None;
        }
    }
    Some(prefix)
}

/// herdr `parse_key_combo`.
pub fn parse_key_combo(s: &str) -> Option<KeyCombo> {
    let parts: Vec<&str> = s.split('+').collect();
    let mut modifiers = KeyModifiers::empty();
    let mut key_str: Option<&str> = None;

    for part in &parts {
        let trimmed = part.trim();
        if trimmed.is_empty() {
            return None;
        }
        if let Some(modifier) = parse_modifier_token(trimmed) {
            modifiers |= modifier;
        } else if key_str.is_some() {
            return None;
        } else {
            key_str = Some(trimmed);
        }
    }

    let key_str = key_str?;
    let single_char = single_key_char(key_str);
    let lower = key_str.to_lowercase();
    let code = match lower.as_str() {
        "space" | " " => KeyCode::Char(' '),
        "enter" | "return" => KeyCode::Enter,
        "esc" | "escape" => KeyCode::Esc,
        "tab" if modifiers.contains(KeyModifiers::SHIFT) => {
            modifiers.remove(KeyModifiers::SHIFT);
            KeyCode::BackTab
        }
        "tab" => KeyCode::Tab,
        "backspace" | "bs" => KeyCode::Backspace,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "minus" => KeyCode::Char('-'),
        "comma" => KeyCode::Char(','),
        "period" => KeyCode::Char('.'),
        "slash" => KeyCode::Char('/'),
        "backslash" => KeyCode::Char('\\'),
        "quote" => KeyCode::Char('\''),
        "double_quote" | "double-quote" => KeyCode::Char('"'),
        "semicolon" => KeyCode::Char(';'),
        "colon" => KeyCode::Char(':'),
        "percent" => KeyCode::Char('%'),
        "ampersand" => KeyCode::Char('&'),
        "backtick" => KeyCode::Char('`'),
        "plus" => KeyCode::Char('+'),
        _ if single_char.is_some() => {
            let ch = single_char?;
            if ch.is_ascii_uppercase() {
                modifiers |= KeyModifiers::SHIFT;
                KeyCode::Char(ch.to_ascii_lowercase())
            } else {
                KeyCode::Char(ch)
            }
        }
        s if s.starts_with('f') => s[1..].parse::<u8>().ok().map(KeyCode::F)?,
        _ => return None,
    };

    Some(normalize_key_combo((code, modifiers)))
}

fn single_key_char(s: &str) -> Option<char> {
    let mut chars = s.chars();
    let ch = chars.next()?;
    if chars.next().is_none() {
        Some(ch)
    } else {
        None
    }
}

fn parse_modifier_token(token: &str) -> Option<KeyModifiers> {
    match token.to_lowercase().as_str() {
        "ctrl" | "control" => Some(KeyModifiers::CONTROL),
        "shift" => Some(KeyModifiers::SHIFT),
        "alt" | "option" | "meta" => Some(KeyModifiers::ALT),
        "cmd" | "command" | "super" => Some(KeyModifiers::SUPER),
        "hyper" => Some(KeyModifiers::HYPER),
        _ => None,
    }
}

fn parse_range_modifiers(s: &str) -> Option<KeyModifiers> {
    let mut modifiers = KeyModifiers::empty();
    let mut saw_range = false;
    for part in s.split('+') {
        let trimmed = part.trim();
        if trimmed == "1..9" {
            if saw_range {
                return None;
            }
            saw_range = true;
        } else {
            modifiers |= parse_modifier_token(trimmed)?;
        }
    }
    saw_range.then_some(modifiers)
}

/// herdr `format_key_combo`.
pub fn format_key_combo(binding: KeyCombo) -> String {
    let (code, modifiers) = binding;
    let mut parts = Vec::new();
    if modifiers.contains(KeyModifiers::CONTROL) {
        parts.push("ctrl".to_string());
    }
    if modifiers.contains(KeyModifiers::ALT) {
        parts.push("alt".to_string());
    }
    if modifiers.contains(KeyModifiers::SHIFT) && !matches!(code, KeyCode::BackTab) {
        parts.push("shift".to_string());
    }
    if modifiers.contains(KeyModifiers::SUPER) {
        parts.push(super_modifier_label().to_string());
    }
    if modifiers.contains(KeyModifiers::HYPER) {
        parts.push("hyper".to_string());
    }
    if modifiers.contains(KeyModifiers::META) {
        parts.push("meta".to_string());
    }

    let key = match code {
        KeyCode::Char(' ') => "space".to_string(),
        KeyCode::Char(c) => c.to_string(),
        KeyCode::Enter => "enter".to_string(),
        KeyCode::Esc => "esc".to_string(),
        KeyCode::Tab => "tab".to_string(),
        KeyCode::BackTab => "shift+tab".to_string(),
        KeyCode::Backspace => "backspace".to_string(),
        KeyCode::Left => "left".to_string(),
        KeyCode::Right => "right".to_string(),
        KeyCode::Up => "up".to_string(),
        KeyCode::Down => "down".to_string(),
        KeyCode::F(n) => format!("f{n}"),
        _ => format!("{:?}", code).to_lowercase(),
    };

    if matches!(code, KeyCode::BackTab) {
        return if parts.is_empty() {
            key
        } else {
            format!("{}+{key}", parts.join("+"))
        };
    }

    parts.push(key);
    parts.join("+")
}

fn super_modifier_label() -> &'static str {
    if cfg!(target_os = "macos") {
        "cmd"
    } else {
        "super"
    }
}

/// herdr `normalize_key_combo`.
pub fn normalize_key_combo(combo: KeyCombo) -> KeyCombo {
    let (mut code, mut modifiers) = combo;
    if matches!(code, KeyCode::Tab) && modifiers.contains(KeyModifiers::SHIFT) {
        code = KeyCode::BackTab;
        modifiers.remove(KeyModifiers::SHIFT);
    } else if matches!(code, KeyCode::BackTab) {
        modifiers.remove(KeyModifiers::SHIFT);
    }
    (code, modifiers)
}

/// herdr `key_event_matches_combo`.
pub fn key_event_matches_combo(key: &KeyEvent, combo: KeyCombo) -> bool {
    let (actual_code, actual_modifiers) = normalize_key_combo((key.code, key.modifiers));
    let (expected_code, expected_modifiers) = normalize_key_combo(combo);

    if actual_modifiers == expected_modifiers
        && key_codes_match(
            actual_code,
            actual_modifiers,
            expected_code,
            expected_modifiers,
        )
    {
        return true;
    }

    let actual_without_shift = actual_modifiers.difference(KeyModifiers::SHIFT);
    actual_modifiers.contains(KeyModifiers::SHIFT)
        && actual_without_shift == expected_modifiers
        && shifted_char_matches_expected(actual_code, expected_code)
        || legacy_shifted_ascii_letter_matches(
            actual_code,
            actual_modifiers,
            expected_code,
            expected_modifiers,
        )
}

fn key_codes_match(
    actual: KeyCode,
    actual_modifiers: KeyModifiers,
    expected: KeyCode,
    expected_modifiers: KeyModifiers,
) -> bool {
    match (actual, expected) {
        (KeyCode::Char(actual), KeyCode::Char(expected))
            if actual.is_ascii_alphabetic() && expected.is_ascii_alphabetic() =>
        {
            actual == expected
                || actual_modifiers.contains(KeyModifiers::SHIFT)
                    && expected_modifiers.contains(KeyModifiers::SHIFT)
                    && actual.eq_ignore_ascii_case(&expected)
        }
        (KeyCode::Char(actual), KeyCode::Char(expected)) => {
            actual == expected
                || shifted_char_matches_expected(KeyCode::Char(actual), KeyCode::Char(expected))
        }
        (actual, expected) => actual == expected,
    }
}

fn legacy_shifted_ascii_letter_matches(
    actual_code: KeyCode,
    actual_modifiers: KeyModifiers,
    expected_code: KeyCode,
    expected_modifiers: KeyModifiers,
) -> bool {
    if actual_modifiers.contains(KeyModifiers::SHIFT) {
        return false;
    }
    let (KeyCode::Char(actual), KeyCode::Char(expected)) = (actual_code, expected_code) else {
        return false;
    };
    actual.is_ascii_uppercase()
        && expected.is_ascii_lowercase()
        && actual.to_ascii_lowercase() == expected
        && actual_modifiers | KeyModifiers::SHIFT == expected_modifiers
}

/// herdr `shifted_char_matches_expected` for a `KeyEvent`, which carries no
/// shifted codepoint: shifted punctuation matches with or without SHIFT.
fn shifted_char_matches_expected(actual_code: KeyCode, expected_code: KeyCode) -> bool {
    let KeyCode::Char(expected) = expected_code else {
        return false;
    };
    matches!(actual_code, KeyCode::Char(actual) if actual == expected && is_shifted_punctuation(expected))
}

fn is_shifted_punctuation(ch: char) -> bool {
    "!@#$%^&*()_+{}|:\"<>?~".contains(ch)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn indexed_override_takes_single_chords_in_order() {
        let keymap =
            Keymap::from_toml("[bindings]\nswitch_project = [\"prefix+f5\", \"prefix+f6\"]\n")
                .unwrap();
        let chords = &keymap.binding("switch_project").unwrap().chords;
        assert_eq!(chords[0].action, Action::SwitchProject(1));
        assert_eq!(chords[1].action, Action::SwitchProject(2));
        assert_eq!(chords[1].label, "prefix+f6");
        let help = keymap.help_entries();
        let tab = help.iter().find(|e| e.name == "switch_tab").unwrap();
        assert_eq!(tab.keys, "prefix+1..9");
        let unset = help.iter().find(|e| e.name == "last_pane").unwrap();
        assert_eq!(unset.keys, "unset");
        assert!(matches!(
            Keymap::from_toml("[bindings]\nzoom = \"prefix+1..9\"\n"),
            Err(KeymapError::InvalidChord { .. })
        ));
    }
}
