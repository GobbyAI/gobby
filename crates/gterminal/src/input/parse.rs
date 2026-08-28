use crossterm::event::{KeyCode, KeyModifiers, MediaKeyCode, ModifierKeyCode};

use super::TerminalKey;

#[allow(dead_code)] // Next step: raw stdin parser will feed TerminalKey directly through this path.
pub fn parse_terminal_key_sequence(data: &str) -> Option<TerminalKey> {
    parse_kitty_key_sequence(data)
        .or_else(|| parse_modify_other_keys_sequence(data))
        .or_else(|| parse_legacy_key_sequence(data))
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn parse_kitty_key_sequence(data: &str) -> Option<TerminalKey> {
    let body = data.strip_prefix("\x1b[")?.strip_suffix('u')?;

    let mut fields = body.split(';');
    let key_part = fields.next()?;
    let modifier_part = fields.next().unwrap_or("1");
    let associated_text = fields.next();
    if fields.next().is_some() {
        return None;
    }

    let (modifier_text, event_type) = split_modifier_and_event(modifier_part);
    let modifier = modifier_text.parse::<u8>().ok()?.checked_sub(1)?;

    let mut key_fields = key_part.split(':');
    let codepoint = key_fields.next()?.parse::<u32>().ok()?;
    let shifted_codepoint = key_fields
        .next()
        .filter(|field| !field.is_empty())
        .and_then(|field| field.parse::<u32>().ok());

    if let Some(text) = associated_text {
        if text.parse::<u32>().ok()? != codepoint {
            return None;
        }
    }

    let code = kitty_codepoint_to_keycode(codepoint)?;
    let kind = parse_kitty_event_type(event_type)?;

    let mut key = TerminalKey::new(code, key_modifiers_from_u8(modifier)).with_kind(kind);
    if let Some(shifted_codepoint) = shifted_codepoint {
        key = key.with_shifted_codepoint(shifted_codepoint);
    }
    Some(key)
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn parse_modify_other_keys_sequence(data: &str) -> Option<TerminalKey> {
    let body = data.strip_prefix("\x1b[27;")?.strip_suffix('~')?;
    let (modifier_part, codepoint_part) = body.split_once(';')?;
    let modifier = modifier_part.parse::<u8>().ok()?.checked_sub(1)?;
    let codepoint = codepoint_part.parse::<u32>().ok()?;

    Some(TerminalKey::new(
        kitty_codepoint_to_keycode(codepoint)?,
        key_modifiers_from_u8(modifier),
    ))
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn parse_legacy_key_sequence(data: &str) -> Option<TerminalKey> {
    if let Some(key) = parse_legacy_special_sequence(data) {
        return Some(key);
    }

    match data {
        // In raw mode, Enter is carriage return. A bare line feed is ambiguous in
        // terminal input and is commonly used for Ctrl+J or Shift+Enter workarounds.
        // Preserve LF by letting it fall through to legacy control-byte parsing.
        "\r" => Some(TerminalKey::new(KeyCode::Enter, KeyModifiers::empty())),
        "\t" => Some(TerminalKey::new(KeyCode::Tab, KeyModifiers::empty())),
        "\x1b" => Some(TerminalKey::new(KeyCode::Esc, KeyModifiers::empty())),
        "\x1b\x7f" => Some(TerminalKey::new(KeyCode::Backspace, KeyModifiers::ALT)),
        "\x7f" => Some(TerminalKey::new(KeyCode::Backspace, KeyModifiers::empty())),
        _ if data.starts_with('\x1b') => {
            let rest = data.strip_prefix('\x1b')?;
            if rest.chars().count() == 1 {
                let ch = rest.chars().next()?;
                let mut modifiers = KeyModifiers::ALT;
                if ch.is_ascii_uppercase() {
                    modifiers |= KeyModifiers::SHIFT;
                }
                Some(TerminalKey::new(KeyCode::Char(ch), modifiers))
            } else {
                None
            }
        }
        _ if data.chars().count() == 1 => {
            let ch = data.chars().next()?;

            if let Some(ctrl_key) = parse_legacy_ctrl_char(ch) {
                return Some(ctrl_key);
            }

            let mut modifiers = KeyModifiers::empty();
            let code = if ch.is_ascii_uppercase() {
                modifiers |= KeyModifiers::SHIFT;
                KeyCode::Char(ch)
            } else {
                KeyCode::Char(ch)
            };
            Some(TerminalKey::new(code, modifiers))
        }
        _ => None,
    }
}

fn parse_legacy_ctrl_char(ch: char) -> Option<TerminalKey> {
    match ch as u32 {
        0 => Some(TerminalKey::new(KeyCode::Char(' '), KeyModifiers::CONTROL)),
        1..=26 => Some(TerminalKey::new(
            KeyCode::Char(char::from_u32((ch as u32) + 96)?),
            KeyModifiers::CONTROL,
        )),
        27 => Some(TerminalKey::new(KeyCode::Char('['), KeyModifiers::CONTROL)),
        28 => Some(TerminalKey::new(KeyCode::Char('\\'), KeyModifiers::CONTROL)),
        29 => Some(TerminalKey::new(KeyCode::Char(']'), KeyModifiers::CONTROL)),
        30 => Some(TerminalKey::new(KeyCode::Char('^'), KeyModifiers::CONTROL)),
        31 => Some(TerminalKey::new(KeyCode::Char('_'), KeyModifiers::CONTROL)),
        _ => None,
    }
}

fn parse_legacy_special_sequence(data: &str) -> Option<TerminalKey> {
    match data {
        "\x1b\x1b[A" => Some(TerminalKey::new(KeyCode::Up, KeyModifiers::ALT)),
        "\x1b\x1b[B" => Some(TerminalKey::new(KeyCode::Down, KeyModifiers::ALT)),
        "\x1b\x1b[C" => Some(TerminalKey::new(KeyCode::Right, KeyModifiers::ALT)),
        "\x1b\x1b[D" => Some(TerminalKey::new(KeyCode::Left, KeyModifiers::ALT)),
        "\x1b[A" | "\x1bOA" => Some(TerminalKey::new(KeyCode::Up, KeyModifiers::empty())),
        "\x1b[B" | "\x1bOB" => Some(TerminalKey::new(KeyCode::Down, KeyModifiers::empty())),
        "\x1b[C" | "\x1bOC" => Some(TerminalKey::new(KeyCode::Right, KeyModifiers::empty())),
        "\x1b[D" | "\x1bOD" => Some(TerminalKey::new(KeyCode::Left, KeyModifiers::empty())),
        "\x1b[H" | "\x1bOH" | "\x1b[1~" | "\x1b[7~" => {
            Some(TerminalKey::new(KeyCode::Home, KeyModifiers::empty()))
        }
        "\x1b[F" | "\x1bOF" | "\x1b[4~" | "\x1b[8~" => {
            Some(TerminalKey::new(KeyCode::End, KeyModifiers::empty()))
        }
        "\x1b[5~" => Some(TerminalKey::new(KeyCode::PageUp, KeyModifiers::empty())),
        "\x1b[6~" => Some(TerminalKey::new(KeyCode::PageDown, KeyModifiers::empty())),
        "\x1b[2~" => Some(TerminalKey::new(KeyCode::Insert, KeyModifiers::empty())),
        "\x1b[3~" => Some(TerminalKey::new(KeyCode::Delete, KeyModifiers::empty())),
        "\x1bOp" => Some(TerminalKey::new(KeyCode::Char('0'), KeyModifiers::empty())),
        "\x1bOq" => Some(TerminalKey::new(KeyCode::Char('1'), KeyModifiers::empty())),
        "\x1bOr" => Some(TerminalKey::new(KeyCode::Char('2'), KeyModifiers::empty())),
        "\x1bOs" => Some(TerminalKey::new(KeyCode::Char('3'), KeyModifiers::empty())),
        "\x1bOt" => Some(TerminalKey::new(KeyCode::Char('4'), KeyModifiers::empty())),
        "\x1bOu" => Some(TerminalKey::new(KeyCode::Char('5'), KeyModifiers::empty())),
        "\x1bOv" => Some(TerminalKey::new(KeyCode::Char('6'), KeyModifiers::empty())),
        "\x1bOw" => Some(TerminalKey::new(KeyCode::Char('7'), KeyModifiers::empty())),
        "\x1bOx" => Some(TerminalKey::new(KeyCode::Char('8'), KeyModifiers::empty())),
        "\x1bOy" => Some(TerminalKey::new(KeyCode::Char('9'), KeyModifiers::empty())),
        "\x1bOn" => Some(TerminalKey::new(KeyCode::Char('.'), KeyModifiers::empty())),
        "\x1bOl" => Some(TerminalKey::new(KeyCode::Char(','), KeyModifiers::empty())),
        "\x1bOm" => Some(TerminalKey::new(KeyCode::Char('-'), KeyModifiers::empty())),
        "\x1bOk" => Some(TerminalKey::new(KeyCode::Char('+'), KeyModifiers::empty())),
        "\x1bOj" => Some(TerminalKey::new(KeyCode::Char('*'), KeyModifiers::empty())),
        "\x1bOo" => Some(TerminalKey::new(KeyCode::Char('/'), KeyModifiers::empty())),
        "\x1bOM" => Some(TerminalKey::new(KeyCode::Enter, KeyModifiers::empty())),
        "\x1bOP" | "\x1b[11~" => Some(TerminalKey::new(KeyCode::F(1), KeyModifiers::empty())),
        "\x1bOQ" | "\x1b[12~" => Some(TerminalKey::new(KeyCode::F(2), KeyModifiers::empty())),
        "\x1bOR" | "\x1b[13~" => Some(TerminalKey::new(KeyCode::F(3), KeyModifiers::empty())),
        "\x1bOS" | "\x1b[14~" => Some(TerminalKey::new(KeyCode::F(4), KeyModifiers::empty())),
        "\x1b[15~" => Some(TerminalKey::new(KeyCode::F(5), KeyModifiers::empty())),
        "\x1b[17~" => Some(TerminalKey::new(KeyCode::F(6), KeyModifiers::empty())),
        "\x1b[18~" => Some(TerminalKey::new(KeyCode::F(7), KeyModifiers::empty())),
        "\x1b[19~" => Some(TerminalKey::new(KeyCode::F(8), KeyModifiers::empty())),
        "\x1b[20~" => Some(TerminalKey::new(KeyCode::F(9), KeyModifiers::empty())),
        "\x1b[21~" => Some(TerminalKey::new(KeyCode::F(10), KeyModifiers::empty())),
        "\x1b[23~" => Some(TerminalKey::new(KeyCode::F(11), KeyModifiers::empty())),
        "\x1b[24~" => Some(TerminalKey::new(KeyCode::F(12), KeyModifiers::empty())),
        "\x1b[Z" => Some(TerminalKey::new(KeyCode::BackTab, KeyModifiers::SHIFT)),
        _ => parse_xterm_modified_special_sequence(data),
    }
}

fn parse_xterm_modified_special_sequence(data: &str) -> Option<TerminalKey> {
    let body = data.strip_prefix("\x1b[")?;

    if let Some(body) = body.strip_prefix("1;") {
        let suffix_char = body.chars().last()?;
        if suffix_char.is_ascii_alphabetic() {
            let modifier_and_event = body.strip_suffix(suffix_char)?;
            let (modifier_text, event_type) = split_modifier_and_event(modifier_and_event);
            let mod_value = modifier_text.parse::<u8>().ok()?.checked_sub(1)?;
            let code = match suffix_char {
                'A' => KeyCode::Up,
                'B' => KeyCode::Down,
                'C' => KeyCode::Right,
                'D' => KeyCode::Left,
                'H' => KeyCode::Home,
                'F' => KeyCode::End,
                'P' => KeyCode::F(1),
                'Q' => KeyCode::F(2),
                'R' => KeyCode::F(3),
                'S' => KeyCode::F(4),
                _ => return None,
            };
            return Some(
                TerminalKey::new(code, key_modifiers_from_u8(mod_value))
                    .with_kind(parse_kitty_event_type(event_type)?),
            );
        }
    }

    let tilde_body = body.strip_suffix('~')?;
    let (code_part, modifier_part) = tilde_body.split_once(';')?;
    let (modifier_text, event_type) = split_modifier_and_event(modifier_part);
    let mod_value = modifier_text.parse::<u8>().ok()?.checked_sub(1)?;
    let code = match code_part {
        "2" => KeyCode::Insert,
        "3" => KeyCode::Delete,
        "5" => KeyCode::PageUp,
        "6" => KeyCode::PageDown,
        "15" => KeyCode::F(5),
        "17" => KeyCode::F(6),
        "18" => KeyCode::F(7),
        "19" => KeyCode::F(8),
        "20" => KeyCode::F(9),
        "21" => KeyCode::F(10),
        "23" => KeyCode::F(11),
        "24" => KeyCode::F(12),
        _ => return None,
    };
    Some(
        TerminalKey::new(code, key_modifiers_from_u8(mod_value))
            .with_kind(parse_kitty_event_type(event_type)?),
    )
}

fn split_modifier_and_event(input: &str) -> (&str, Option<&str>) {
    match input.split_once(':') {
        Some((modifier, event)) if !modifier.is_empty() => (modifier, Some(event)),
        _ => (input, None),
    }
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn parse_kitty_event_type(value: Option<&str>) -> Option<crossterm::event::KeyEventKind> {
    match value.unwrap_or("1") {
        "1" => Some(crossterm::event::KeyEventKind::Press),
        "2" => Some(crossterm::event::KeyEventKind::Repeat),
        "3" => Some(crossterm::event::KeyEventKind::Release),
        _ => None,
    }
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn kitty_codepoint_to_keycode(codepoint: u32) -> Option<KeyCode> {
    match codepoint {
        8 | 127 => Some(KeyCode::Backspace),
        9 => Some(KeyCode::Tab),
        13 | 57414 => Some(KeyCode::Enter),
        27 => Some(KeyCode::Esc),
        57358 => Some(KeyCode::CapsLock),
        57359 => Some(KeyCode::ScrollLock),
        57360 => Some(KeyCode::NumLock),
        57361 => Some(KeyCode::PrintScreen),
        57362 => Some(KeyCode::Pause),
        57363 => Some(KeyCode::Menu),
        57376..=57398 => Some(KeyCode::F((codepoint - 57376 + 13) as u8)),
        57399 => Some(KeyCode::Char('0')),
        57400 => Some(KeyCode::Char('1')),
        57401 => Some(KeyCode::Char('2')),
        57402 => Some(KeyCode::Char('3')),
        57403 => Some(KeyCode::Char('4')),
        57404 => Some(KeyCode::Char('5')),
        57405 => Some(KeyCode::Char('6')),
        57406 => Some(KeyCode::Char('7')),
        57407 => Some(KeyCode::Char('8')),
        57408 => Some(KeyCode::Char('9')),
        57409 => Some(KeyCode::Char('.')),
        57410 => Some(KeyCode::Char('/')),
        57411 => Some(KeyCode::Char('*')),
        57412 => Some(KeyCode::Char('-')),
        57413 => Some(KeyCode::Char('+')),
        57415 => Some(KeyCode::Char('=')),
        57416 => Some(KeyCode::Char(',')),
        57417 => Some(KeyCode::Left),
        57418 => Some(KeyCode::Right),
        57419 => Some(KeyCode::Up),
        57420 => Some(KeyCode::Down),
        57421 => Some(KeyCode::PageUp),
        57422 => Some(KeyCode::PageDown),
        57423 => Some(KeyCode::Home),
        57424 => Some(KeyCode::End),
        57425 => Some(KeyCode::Insert),
        57426 => Some(KeyCode::Delete),
        57427 => Some(KeyCode::KeypadBegin),
        57428 => Some(KeyCode::Media(MediaKeyCode::Play)),
        57429 => Some(KeyCode::Media(MediaKeyCode::Pause)),
        57430 => Some(KeyCode::Media(MediaKeyCode::PlayPause)),
        57431 => Some(KeyCode::Media(MediaKeyCode::Reverse)),
        57432 => Some(KeyCode::Media(MediaKeyCode::Stop)),
        57433 => Some(KeyCode::Media(MediaKeyCode::FastForward)),
        57434 => Some(KeyCode::Media(MediaKeyCode::Rewind)),
        57435 => Some(KeyCode::Media(MediaKeyCode::TrackNext)),
        57436 => Some(KeyCode::Media(MediaKeyCode::TrackPrevious)),
        57437 => Some(KeyCode::Media(MediaKeyCode::Record)),
        57438 => Some(KeyCode::Media(MediaKeyCode::LowerVolume)),
        57439 => Some(KeyCode::Media(MediaKeyCode::RaiseVolume)),
        57440 => Some(KeyCode::Media(MediaKeyCode::MuteVolume)),
        57441 => Some(KeyCode::Modifier(ModifierKeyCode::LeftShift)),
        57442 => Some(KeyCode::Modifier(ModifierKeyCode::LeftControl)),
        57443 => Some(KeyCode::Modifier(ModifierKeyCode::LeftAlt)),
        57444 => Some(KeyCode::Modifier(ModifierKeyCode::LeftSuper)),
        57445 => Some(KeyCode::Modifier(ModifierKeyCode::LeftHyper)),
        57446 => Some(KeyCode::Modifier(ModifierKeyCode::LeftMeta)),
        57447 => Some(KeyCode::Modifier(ModifierKeyCode::RightShift)),
        57448 => Some(KeyCode::Modifier(ModifierKeyCode::RightControl)),
        57449 => Some(KeyCode::Modifier(ModifierKeyCode::RightAlt)),
        57450 => Some(KeyCode::Modifier(ModifierKeyCode::RightSuper)),
        57451 => Some(KeyCode::Modifier(ModifierKeyCode::RightHyper)),
        57452 => Some(KeyCode::Modifier(ModifierKeyCode::RightMeta)),
        57453 => Some(KeyCode::Modifier(ModifierKeyCode::IsoLevel3Shift)),
        57454 => Some(KeyCode::Modifier(ModifierKeyCode::IsoLevel5Shift)),
        value if is_kitty_functional_codepoint(value) => None,
        value => char::from_u32(value).map(KeyCode::Char),
    }
}

fn is_kitty_functional_codepoint(codepoint: u32) -> bool {
    (57358..=57454).contains(&codepoint)
}

#[allow(dead_code)] // Reserved for the upcoming raw stdin parser.
fn key_modifiers_from_u8(modifier: u8) -> KeyModifiers {
    let mut mods = KeyModifiers::empty();
    if modifier & 0b0000_0001 != 0 {
        mods |= KeyModifiers::SHIFT;
    }
    if modifier & 0b0000_0010 != 0 {
        mods |= KeyModifiers::ALT;
    }
    if modifier & 0b0000_0100 != 0 {
        mods |= KeyModifiers::CONTROL;
    }
    if modifier & 0b0000_1000 != 0 {
        mods |= KeyModifiers::SUPER;
    }
    if modifier & 0b0001_0000 != 0 {
        mods |= KeyModifiers::HYPER;
    }
    if modifier & 0b0010_0000 != 0 {
        mods |= KeyModifiers::META;
    }
    mods
}

#[cfg(test)]
#[path = "parse/tests.rs"]
mod tests;
