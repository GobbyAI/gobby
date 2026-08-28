use crossterm::event::{KeyCode, KeyModifiers, ModifierKeyCode};

use super::*;
use crate::input::{encode_terminal_key, KeyboardProtocol};

fn assert_terminal_key_eq(
    actual: TerminalKey,
    code: KeyCode,
    modifiers: KeyModifiers,
    kind: crossterm::event::KeyEventKind,
    shifted_codepoint: Option<u32>,
) {
    assert_eq!(actual.code, code);
    assert_eq!(actual.modifiers, modifiers);
    assert_eq!(actual.kind, kind);
    assert_eq!(actual.shifted_codepoint, shifted_codepoint);
}

fn decode_hex(hex: &str) -> Vec<u8> {
    let hex = hex.trim();
    assert_eq!(hex.len() % 2, 0, "hex string must have even length");
    (0..hex.len())
        .step_by(2)
        .map(|idx| u8::from_str_radix(&hex[idx..idx + 2], 16).unwrap())
        .collect()
}

fn parse_fixture_key_code(value: &str) -> KeyCode {
    match value {
        "enter" => KeyCode::Enter,
        "tab" => KeyCode::Tab,
        "backspace" => KeyCode::Backspace,
        "esc" => KeyCode::Esc,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "home" => KeyCode::Home,
        "end" => KeyCode::End,
        "pageup" => KeyCode::PageUp,
        "pagedown" => KeyCode::PageDown,
        "insert" => KeyCode::Insert,
        "delete" => KeyCode::Delete,
        value if value.starts_with("char:") => {
            KeyCode::Char(value.trim_start_matches("char:").chars().next().unwrap())
        }
        other => panic!("unsupported fixture key code: {other}"),
    }
}

fn parse_fixture_modifiers(value: &str) -> KeyModifiers {
    if value == "-" || value.is_empty() {
        return KeyModifiers::empty();
    }

    let mut modifiers = KeyModifiers::empty();
    for part in value.split('+') {
        match part {
            "shift" => modifiers |= KeyModifiers::SHIFT,
            "alt" => modifiers |= KeyModifiers::ALT,
            "control" => modifiers |= KeyModifiers::CONTROL,
            "super" => modifiers |= KeyModifiers::SUPER,
            "hyper" => modifiers |= KeyModifiers::HYPER,
            "meta" => modifiers |= KeyModifiers::META,
            other => panic!("unsupported fixture modifier: {other}"),
        }
    }
    modifiers
}

fn parse_fixture_kind(value: &str) -> crossterm::event::KeyEventKind {
    match value {
        "press" => crossterm::event::KeyEventKind::Press,
        "repeat" => crossterm::event::KeyEventKind::Repeat,
        "release" => crossterm::event::KeyEventKind::Release,
        other => panic!("unsupported fixture kind: {other}"),
    }
}

#[test]
fn parse_legacy_f_keys() {
    let cases = [
        ("\x1bOP", KeyCode::F(1)),
        ("\x1b[11~", KeyCode::F(1)),
        ("\x1bOQ", KeyCode::F(2)),
        ("\x1b[12~", KeyCode::F(2)),
        ("\x1bOR", KeyCode::F(3)),
        ("\x1b[13~", KeyCode::F(3)),
        ("\x1bOS", KeyCode::F(4)),
        ("\x1b[14~", KeyCode::F(4)),
    ];

    for (sequence, code) in cases {
        assert_terminal_key_eq(
            parse_terminal_key_sequence(sequence).expect("f key should parse"),
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Press,
            None,
        );
    }

    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[15~").expect("f5 should parse"),
        KeyCode::F(5),
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_eq!(parse_terminal_key_sequence("\x1b[10~"), None);
    assert_eq!(parse_terminal_key_sequence("\x1b[16~"), None);
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[1~").expect("home should parse"),
        KeyCode::Home,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[4~").expect("end should parse"),
        KeyCode::End,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[5~").expect("pageup should parse"),
        KeyCode::PageUp,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[6~").expect("pagedown should parse"),
        KeyCode::PageDown,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
}

#[test]
fn parse_legacy_application_keypad_sequences() {
    let cases = [
        ("\x1bOp", KeyCode::Char('0')),
        ("\x1bOq", KeyCode::Char('1')),
        ("\x1bOr", KeyCode::Char('2')),
        ("\x1bOs", KeyCode::Char('3')),
        ("\x1bOt", KeyCode::Char('4')),
        ("\x1bOu", KeyCode::Char('5')),
        ("\x1bOv", KeyCode::Char('6')),
        ("\x1bOw", KeyCode::Char('7')),
        ("\x1bOx", KeyCode::Char('8')),
        ("\x1bOy", KeyCode::Char('9')),
        ("\x1bOn", KeyCode::Char('.')),
        ("\x1bOl", KeyCode::Char(',')),
        ("\x1bOm", KeyCode::Char('-')),
        ("\x1bOk", KeyCode::Char('+')),
        ("\x1bOj", KeyCode::Char('*')),
        ("\x1bOo", KeyCode::Char('/')),
        ("\x1bOM", KeyCode::Enter),
    ];

    for (sequence, code) in cases {
        assert_terminal_key_eq(
            parse_terminal_key_sequence(sequence).expect("keypad sequence should parse"),
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Press,
            None,
        );
    }
}

#[test]
fn parse_legacy_alt_shift_letter_preserves_shift() {
    let key = parse_terminal_key_sequence("\x1bA").expect("alt-shift letter should parse");
    assert_terminal_key_eq(
        key.clone(),
        KeyCode::Char('A'),
        KeyModifiers::ALT | KeyModifiers::SHIFT,
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_eq!(encode_terminal_key(key, KeyboardProtocol::Legacy), b"\x1bA");
}

#[test]
fn unknown_legacy_ss3_sequence_remains_unsupported() {
    assert!(parse_terminal_key_sequence("\x1bOz").is_none());
}

#[test]
fn parse_modified_f_keys() {
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[1;2P").expect("shift+f1 should parse"),
        KeyCode::F(1),
        KeyModifiers::SHIFT,
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[1;3S").expect("alt+f4 should parse"),
        KeyCode::F(4),
        KeyModifiers::ALT,
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[1;4S").expect("shift+alt+f4 should parse"),
        KeyCode::F(4),
        KeyModifiers::SHIFT | KeyModifiers::ALT,
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_terminal_key_eq(
        parse_terminal_key_sequence("\x1b[15;2~").expect("shift+f5 should parse"),
        KeyCode::F(5),
        KeyModifiers::SHIFT,
        crossterm::event::KeyEventKind::Press,
        None,
    );
    assert_eq!(parse_terminal_key_sequence("\x1b[11;2~"), None);
    assert_eq!(parse_terminal_key_sequence("\x1b[14;1~"), None);
    assert_eq!(parse_terminal_key_sequence("\x1b[14;3~"), None);
}

#[test]
fn parse_kitty_sequence_preserves_shifted_symbol_pair() {
    let key = parse_terminal_key_sequence("\x1b[49:33;2:1u").unwrap();
    assert_eq!(key.code, KeyCode::Char('1'));
    assert_eq!(key.modifiers, KeyModifiers::SHIFT);
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
    assert_eq!(key.shifted_codepoint, Some('!' as u32));
}

#[test]
fn parse_kitty_sequence_preserves_shifted_letter_pair_and_release() {
    let key = parse_terminal_key_sequence("\x1b[108:76;2:3u").unwrap();
    assert_eq!(key.code, KeyCode::Char('l'));
    assert_eq!(key.modifiers, KeyModifiers::SHIFT);
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Release);
    assert_eq!(key.shifted_codepoint, Some('L' as u32));
}

#[test]
fn parse_kitty_sequence_preserves_non_us_shift_pairs() {
    for (sequence, base, shifted) in [
        ("\x1b[50:34;2:1u", '2', '"'),
        ("\x1b[38:49;2:1u", '&', '1'),
        ("\x1b[305:73;2:1u", 'ı', 'I'),
        ("\x1b[287:286;2:1u", 'ğ', 'Ğ'),
    ] {
        let key = parse_terminal_key_sequence(sequence).unwrap();
        assert_eq!(key.code, KeyCode::Char(base));
        assert_eq!(key.modifiers, KeyModifiers::SHIFT);
        assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
        assert_eq!(key.shifted_codepoint, Some(shifted as u32));
    }
}

#[test]
fn parse_kitty_sequence_with_associated_emoji_text() {
    let key = parse_terminal_key_sequence("\x1b[128512;1;128512u").unwrap();
    assert_terminal_key_eq(
        key.clone(),
        KeyCode::Char('😀'),
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
        None,
    );
}

#[test]
fn reject_unmodeled_kitty_associated_text() {
    assert_eq!(parse_terminal_key_sequence("\x1b[128512;1;128513u"), None);
    assert_eq!(
        parse_terminal_key_sequence("\x1b[128512;1;128512:65039u"),
        None
    );
}

#[test]
fn parse_kitty_alt_backspace_sequence() {
    let key = parse_terminal_key_sequence("\x1b[127;3u").unwrap();
    assert_eq!(key.code, KeyCode::Backspace);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
    assert_eq!(key.shifted_codepoint, None);
}

#[test]
fn parse_modify_other_keys_sequence() {
    let key = parse_terminal_key_sequence("\x1b[27;6;108~").unwrap();
    assert_eq!(key.code, KeyCode::Char('l'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL | KeyModifiers::SHIFT);
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
    assert_eq!(key.shifted_codepoint, None);
}

#[test]
fn parse_legacy_uppercase_letter_as_shifted_char() {
    let key = parse_terminal_key_sequence("L").unwrap();
    assert_eq!(key.code, KeyCode::Char('L'));
    assert_eq!(key.modifiers, KeyModifiers::SHIFT);
}

#[test]
fn parse_legacy_up_arrow_sequence() {
    let key = parse_terminal_key_sequence("\x1b[A").unwrap();
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::empty());
}

#[test]
fn parse_legacy_alt_backspace_sequence() {
    let key = parse_terminal_key_sequence("\x1b\x7f").unwrap();
    assert_eq!(key.code, KeyCode::Backspace);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parse_kitty_modifier_sequence() {
    let key = parse_terminal_key_sequence("\x1b[57441;2:1u").unwrap();
    assert_eq!(key.code, KeyCode::Modifier(ModifierKeyCode::LeftShift));
    assert_eq!(key.modifiers, KeyModifiers::SHIFT);
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
}

#[test]
fn parse_ghostty_enhanced_up_arrow_press_sequence() {
    let key = parse_terminal_key_sequence("\x1b[1;1:1A").unwrap();
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
}

#[test]
fn parse_ghostty_enhanced_up_arrow_release_sequence() {
    let key = parse_terminal_key_sequence("\x1b[1;1:3A").unwrap();
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Release);
}

#[test]
fn parse_ghostty_enhanced_pageup_press_sequence() {
    let key = parse_terminal_key_sequence("\x1b[5;1:1~").unwrap();
    assert_eq!(key.code, KeyCode::PageUp);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Press);
}

#[test]
fn parse_ghostty_enhanced_pagedown_release_sequence() {
    let key = parse_terminal_key_sequence("\x1b[6;1:3~").unwrap();
    assert_eq!(key.code, KeyCode::PageDown);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Release);
}

#[test]
fn parse_ghostty_enhanced_delete_repeat_sequence() {
    let key = parse_terminal_key_sequence("\x1b[3;1:2~").unwrap();
    assert_eq!(key.code, KeyCode::Delete);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, crossterm::event::KeyEventKind::Repeat);
}

#[test]
fn parse_xterm_alt_up_arrow_sequence() {
    let key = parse_terminal_key_sequence("\x1b[1;3A").unwrap();
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parse_xterm_alt_down_arrow_sequence() {
    let key = parse_terminal_key_sequence("\x1b[1;3B").unwrap();
    assert_eq!(key.code, KeyCode::Down);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parse_kitty_functional_up_arrow_sequence() {
    let key = parse_terminal_key_sequence("\x1b[57419;1u").unwrap();
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::empty());
}

#[test]
fn parse_legacy_ctrl_b_sequence() {
    let key = parse_terminal_key_sequence("\x02").unwrap();
    assert_eq!(key.code, KeyCode::Char('b'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL);
}

#[test]
fn parse_legacy_ctrl_c_sequence() {
    let key = parse_terminal_key_sequence("\x03").unwrap();
    assert_eq!(key.code, KeyCode::Char('c'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL);
}

#[test]
fn parse_legacy_lf_sequence_as_ctrl_j() {
    let key = parse_terminal_key_sequence("\n").unwrap();
    assert_eq!(key.code, KeyCode::Char('j'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL);
}

#[test]
fn legacy_lf_roundtrips_as_lf() {
    let key = parse_terminal_key_sequence("\n").unwrap();
    assert_eq!(encode_terminal_key(key, KeyboardProtocol::Legacy), b"\n");
}

#[test]
fn legacy_ctrl_byte_matrix_is_covered() {
    for (byte, expected) in [
        (b'\x01', 'a'),
        (b'\x02', 'b'),
        (b'\x03', 'c'),
        (b'\x1a', 'z'),
    ] {
        let key = parse_terminal_key_sequence(std::str::from_utf8(&[byte]).unwrap()).unwrap();
        assert_terminal_key_eq(
            key,
            KeyCode::Char(expected),
            KeyModifiers::CONTROL,
            crossterm::event::KeyEventKind::Press,
            None,
        );
    }

    for (byte, expected) in [
        (b'\x1c', '\\'),
        (b'\x1d', ']'),
        (b'\x1e', '^'),
        (b'\x1f', '_'),
    ] {
        let key = parse_terminal_key_sequence(std::str::from_utf8(&[byte]).unwrap()).unwrap();
        assert_terminal_key_eq(
            key,
            KeyCode::Char(expected),
            KeyModifiers::CONTROL,
            crossterm::event::KeyEventKind::Press,
            None,
        );
    }
}

#[test]
fn kitty_functional_key_matrix_is_covered() {
    let cases = [
        ("\x1b[57399;1u", KeyCode::Char('0')),
        ("\x1b[57400;1u", KeyCode::Char('1')),
        ("\x1b[57401;1u", KeyCode::Char('2')),
        ("\x1b[57402;1u", KeyCode::Char('3')),
        ("\x1b[57403;1u", KeyCode::Char('4')),
        ("\x1b[57404;1u", KeyCode::Char('5')),
        ("\x1b[57405;1u", KeyCode::Char('6')),
        ("\x1b[57406;1u", KeyCode::Char('7')),
        ("\x1b[57407;1u", KeyCode::Char('8')),
        ("\x1b[57408;1u", KeyCode::Char('9')),
        ("\x1b[57409;1u", KeyCode::Char('.')),
        ("\x1b[57410;1u", KeyCode::Char('/')),
        ("\x1b[57411;1u", KeyCode::Char('*')),
        ("\x1b[57412;1u", KeyCode::Char('-')),
        ("\x1b[57413;1u", KeyCode::Char('+')),
        ("\x1b[57414;1u", KeyCode::Enter),
        ("\x1b[57415;1u", KeyCode::Char('=')),
        ("\x1b[57416;1u", KeyCode::Char(',')),
        ("\x1b[57417;1u", KeyCode::Left),
        ("\x1b[57418;1u", KeyCode::Right),
        ("\x1b[57419;1u", KeyCode::Up),
        ("\x1b[57420;1u", KeyCode::Down),
        ("\x1b[57421;1u", KeyCode::PageUp),
        ("\x1b[57422;1u", KeyCode::PageDown),
        ("\x1b[57423;1u", KeyCode::Home),
        ("\x1b[57424;1u", KeyCode::End),
        ("\x1b[57425;1u", KeyCode::Insert),
        ("\x1b[57426;1u", KeyCode::Delete),
    ];

    for (sequence, code) in cases {
        let parsed = parse_terminal_key_sequence(sequence).unwrap();
        assert_terminal_key_eq(
            parsed,
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Press,
            None,
        );
    }
}

#[test]
fn unknown_kitty_functional_key_remains_unsupported() {
    assert!(parse_terminal_key_sequence("\x1b[57364;1u").is_none());
}

fn assert_fixture_corpus_parses(corpus: &str) {
    for line in corpus.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        let mut columns: Vec<_> = line.split('\t').collect();
        if columns.len() == 5 {
            columns.push("");
        }

        let (family, bytes_hex, code, modifiers, kind, shifted) = match columns.len() {
            6 => {
                if columns[1].chars().all(|ch| ch.is_ascii_hexdigit()) {
                    (
                        columns[0], columns[1], columns[2], columns[3], columns[4], columns[5],
                    )
                } else {
                    (
                        columns[0], columns[2], columns[3], columns[4], columns[5], "",
                    )
                }
            }
            7 => (
                columns[0], columns[2], columns[3], columns[4], columns[5], columns[6],
            ),
            _ => panic!("fixture row must have 6 or 7 columns: {line}"),
        };

        assert!(
            bytes_hex.chars().all(|ch| ch.is_ascii_hexdigit()),
            "non-hex fixture bytes for {family}: {bytes_hex}"
        );
        let bytes = decode_hex(bytes_hex);
        let text = std::str::from_utf8(&bytes).unwrap();
        let parsed = parse_terminal_key_sequence(text)
            .unwrap_or_else(|| panic!("fixture failed to parse: {family}"));

        assert_terminal_key_eq(
            parsed,
            parse_fixture_key_code(code),
            parse_fixture_modifiers(modifiers),
            parse_fixture_kind(kind),
            if shifted.is_empty() {
                None
            } else {
                Some(shifted.parse::<u32>().unwrap())
            },
        );
    }
}

#[test]
fn keyboard_protocol_corpus_fixture_parses() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/keyboard_protocol_corpus.tsv"
    ));
    assert_fixture_corpus_parses(corpus);
}

#[test]
fn macos_terminal_variants_fixture_parses() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/macos_terminal_variants.tsv"
    ));
    for line in corpus.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        let mut columns: Vec<_> = line.split('\t').collect();
        if columns.len() == 6 {
            columns.push("");
        }
        assert_eq!(
            columns.len(),
            7,
            "macOS fixture row must have 7 columns: {line}"
        );

        let source = format!("{}:{}", columns[0], columns[1]);
        let transformed = [
            source.as_str(),
            columns[2],
            columns[3],
            columns[4],
            columns[5],
            columns[6],
        ]
        .join("\t");
        assert_fixture_corpus_parses(&transformed);
    }
}

#[test]
fn linux_terminal_variants_fixture_parses() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/linux_terminal_variants.tsv"
    ));
    assert_fixture_corpus_parses(corpus);
}
