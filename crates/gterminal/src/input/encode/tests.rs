use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use super::*;
use crate::input::parse_terminal_key_sequence;

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

#[test]
fn legacy_enter() {
    let key = KeyEvent::new(KeyCode::Enter, KeyModifiers::empty());
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), vec![b'\r']);
}

#[test]
fn legacy_ctrl_c() {
    let key = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), vec![3]);
}

#[test]
fn legacy_ctrl_slash_aliases_ctrl_underscore() {
    let key = KeyEvent::new(KeyCode::Char('/'), KeyModifiers::CONTROL);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), vec![31]);
}

#[test]
fn legacy_shift_enter_is_just_cr() {
    let key = KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), vec![b'\r']);
}

#[test]
fn legacy_alt_up() {
    let key = KeyEvent::new(KeyCode::Up, KeyModifiers::ALT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[1;3A");
}

#[test]
fn legacy_shift_right() {
    let key = KeyEvent::new(KeyCode::Right, KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[1;2C");
}

#[test]
fn legacy_ctrl_left() {
    let key = KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[1;5D");
}

#[test]
fn legacy_ctrl_shift_end() {
    let key = KeyEvent::new(KeyCode::End, KeyModifiers::CONTROL | KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[1;6F");
}

#[test]
fn legacy_alt_delete() {
    let key = KeyEvent::new(KeyCode::Delete, KeyModifiers::ALT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[3;3~");
}

#[test]
fn legacy_shift_f5() {
    let key = KeyEvent::new(KeyCode::F(5), KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b[15;2~");
}

#[test]
fn legacy_alt_char_still_esc_prefix() {
    let key = KeyEvent::new(KeyCode::Char('a'), KeyModifiers::ALT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1ba");
}

#[test]
fn legacy_alt_shift_punctuation_uses_shifted_text() {
    let key = parse_terminal_key_sequence("\x1b[44:60;4u").unwrap();
    assert_eq!(encode_terminal_key(key, KeyboardProtocol::Legacy), b"\x1b<");
}

#[test]
fn legacy_alt_backspace_sends_escape_delete() {
    let key = KeyEvent::new(KeyCode::Backspace, KeyModifiers::ALT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"\x1b\x7f");
}

#[test]
fn application_cursor_keys_use_ss3_sequences() {
    assert_eq!(encode_cursor_key(KeyCode::Up, true), b"\x1bOA");
    assert_eq!(encode_cursor_key(KeyCode::Down, true), b"\x1bOB");
}

#[test]
fn normal_cursor_keys_use_csi_sequences() {
    assert_eq!(encode_cursor_key(KeyCode::Up, false), b"\x1b[A");
    assert_eq!(encode_cursor_key(KeyCode::Down, false), b"\x1b[B");
}

#[test]
fn sgr_mouse_scroll_encodes_wheel_button_and_coordinates() {
    let encoded = encode_mouse_scroll(
        crossterm::event::MouseEventKind::ScrollDown,
        4,
        6,
        KeyModifiers::SHIFT,
        MouseProtocolEncoding::Sgr,
    )
    .expect("mouse scroll should encode");

    assert_eq!(encoded, b"\x1b[<69;5;7M");
}

#[test]
fn sgr_mouse_release_keeps_button_code() {
    let encoded = encode_mouse_button(
        crossterm::event::MouseEventKind::Up(crossterm::event::MouseButton::Left),
        11,
        9,
        KeyModifiers::empty(),
        MouseProtocolEncoding::Sgr,
    )
    .expect("mouse release should encode");

    assert_eq!(encoded, b"\x1b[<0;12;10m");
}

#[test]
fn kitty_shift_enter() {
    let key = KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[13;2u"
    );
}

#[test]
fn kitty_ctrl_shift_a() {
    let key = KeyEvent::new(
        KeyCode::Char('a'),
        KeyModifiers::CONTROL | KeyModifiers::SHIFT,
    );
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[97;6u"
    );
}

#[test]
fn kitty_shift_uppercase_letter_sends_text() {
    let key = KeyEvent::new(KeyCode::Char('L'), KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Kitty { flags: 1 }), b"L");
}

#[test]
fn kitty_shift_uppercase_letter_ignores_alternate_key_reporting_for_text() {
    let key = KeyEvent::new(KeyCode::Char('L'), KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Kitty { flags: 7 }), b"L");
}

#[test]
fn kitty_shift_lowercase_letter_sends_uppercase_text() {
    let key = KeyEvent::new(KeyCode::Char('l'), KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Kitty { flags: 1 }), b"L");
}

#[test]
fn kitty_alt_shift_uppercase_letter_uses_base_codepoint() {
    let key = KeyEvent::new(KeyCode::Char('L'), KeyModifiers::ALT | KeyModifiers::SHIFT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[108;4u"
    );
}

#[test]
fn kitty_ctrl_shift_uppercase_letter_uses_base_codepoint() {
    let key = KeyEvent::new(
        KeyCode::Char('L'),
        KeyModifiers::CONTROL | KeyModifiers::SHIFT,
    );
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[108;6u"
    );
}

#[test]
fn legacy_shift_uppercase_letter_stays_uppercase() {
    let key = KeyEvent::new(KeyCode::Char('L'), KeyModifiers::SHIFT);
    assert_eq!(encode_key(key, KeyboardProtocol::Legacy), b"L");
}

#[test]
fn kitty_alt_enter() {
    let key = KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[13;3u"
    );
}

#[test]
fn kitty_alt_backspace_uses_csi_u() {
    let key = KeyEvent::new(KeyCode::Backspace, KeyModifiers::ALT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[127;3u"
    );
}

#[test]
fn kitty_plain_ctrl_c_uses_csi_u() {
    let key = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[99;5u"
    );
}

#[test]
fn kitty_plain_ctrl_c_includes_press_event_when_requested() {
    let key = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 3 }),
        b"\x1b[99;5:1u"
    );
}

#[test]
fn kitty_unmodified_uses_legacy() {
    let key = KeyEvent::new(KeyCode::Char('a'), KeyModifiers::empty());
    assert_eq!(encode_key(key, KeyboardProtocol::Kitty { flags: 1 }), b"a");
}

#[test]
fn kitty_report_event_types_keeps_basic_compatibility_keys_legacy() {
    let cases = [
        (KeyCode::Enter, b"\r".as_slice()),
        (KeyCode::Tab, b"\t".as_slice()),
        (KeyCode::Backspace, b"\x7f".as_slice()),
    ];

    for (code, expected) in cases {
        let press = KeyEvent::new_with_kind(
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Press,
        );
        assert_eq!(
            encode_key(press, KeyboardProtocol::Kitty { flags: 3 }),
            expected,
            "{code:?} press should stay legacy-compatible without REPORT_ALL_KEYS"
        );

        let repeat = KeyEvent::new_with_kind(
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Repeat,
        );
        assert_eq!(
            encode_key(repeat, KeyboardProtocol::Kitty { flags: 3 }),
            expected,
            "{code:?} repeat should stay legacy-compatible without REPORT_ALL_KEYS"
        );

        let release = KeyEvent::new_with_kind(
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Release,
        );
        assert_eq!(
            encode_key(release, KeyboardProtocol::Kitty { flags: 3 }),
            b"",
            "{code:?} release should not fall back to legacy bytes"
        );
    }
}

#[test]
fn kitty_report_all_keys_encodes_basic_compatibility_keys_with_events() {
    let enter_press = KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
    );
    assert_eq!(
        encode_key(enter_press, KeyboardProtocol::Kitty { flags: 9 }),
        b"\x1b[13;1u"
    );

    let backspace_press = KeyEvent::new_with_kind(
        KeyCode::Backspace,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Press,
    );
    assert_eq!(
        encode_key(backspace_press, KeyboardProtocol::Kitty { flags: 11 }),
        b"\x1b[127;1:1u"
    );

    let backspace_release = KeyEvent::new_with_kind(
        KeyCode::Backspace,
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Release,
    );
    assert_eq!(
        encode_key(backspace_release, KeyboardProtocol::Kitty { flags: 11 }),
        b"\x1b[127;1:3u"
    );
}

#[test]
fn kitty_report_all_keys_encodes_printable_event_kinds() {
    for (kind, expected) in [
        (
            crossterm::event::KeyEventKind::Press,
            b"\x1b[106;1:1u".as_slice(),
        ),
        (
            crossterm::event::KeyEventKind::Repeat,
            b"\x1b[106;1:2u".as_slice(),
        ),
        (
            crossterm::event::KeyEventKind::Release,
            b"\x1b[106;1:3u".as_slice(),
        ),
    ] {
        let key = KeyEvent::new_with_kind(KeyCode::Char('j'), KeyModifiers::empty(), kind);
        assert_eq!(
            encode_key(key, KeyboardProtocol::Kitty { flags: 15 }),
            expected
        );
    }
}

#[test]
fn kitty_report_associated_text_embeds_shifted_printables() {
    let cases = [
        (
            TerminalKey::new(KeyCode::Char('A'), KeyModifiers::SHIFT),
            b"\x1b[97;2;65u".as_slice(),
        ),
        (
            TerminalKey::new(KeyCode::Char('1'), KeyModifiers::SHIFT)
                .with_shifted_codepoint('!' as u32),
            b"\x1b[49;2;33u".as_slice(),
        ),
        (
            TerminalKey::new(KeyCode::Char(':'), KeyModifiers::SHIFT),
            b"\x1b[58;2;58u".as_slice(),
        ),
    ];

    for (key, expected) in cases {
        assert_eq!(
            encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 25 }),
            expected
        );
    }
}

#[test]
fn kitty_associated_text_composes_with_alternates_and_events() {
    for (kind, expected) in [
        (
            crossterm::event::KeyEventKind::Press,
            b"\x1b[97:65;2:1;65u".as_slice(),
        ),
        (
            crossterm::event::KeyEventKind::Repeat,
            b"\x1b[97:65;2:2;65u".as_slice(),
        ),
        (
            crossterm::event::KeyEventKind::Release,
            b"\x1b[97:65;2:3u".as_slice(),
        ),
    ] {
        let key = TerminalKey::new(KeyCode::Char('A'), KeyModifiers::SHIFT).with_kind(kind);
        assert_eq!(
            encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 31 }),
            expected
        );
    }
}

#[test]
fn kitty_printable_release_is_encoded_without_report_all() {
    let release = KeyEvent::new_with_kind(
        KeyCode::Char('j'),
        KeyModifiers::empty(),
        crossterm::event::KeyEventKind::Release,
    );
    assert_eq!(
        encode_key(release, KeyboardProtocol::Kitty { flags: 3 }),
        b"\x1b[106;1:3u"
    );

    let mut malformed_release = TerminalKey::from(release);
    malformed_release.generated_text = Some("j".to_owned());
    assert_eq!(
        encode_terminal_key(malformed_release, KeyboardProtocol::Kitty { flags: 3 }),
        b"\x1b[106;1:3u"
    );
}

#[test]
fn kitty_shift_tab() {
    let key = KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[9;2u"
    );
}

#[test]
fn kitty_ctrl_shift_enter() {
    let key = KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL | KeyModifiers::SHIFT);
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 1 }),
        b"\x1b[13;6u"
    );
}

#[test]
fn kitty_repeat_event_type_is_encoded_when_requested() {
    let key = KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::SHIFT,
        crossterm::event::KeyEventKind::Repeat,
    );
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 3 }),
        b"\x1b[13;2:2u"
    );
}

#[test]
fn kitty_shift_letter_release_uses_csi_u() {
    let key = KeyEvent::new_with_kind(
        KeyCode::Char('L'),
        KeyModifiers::SHIFT,
        crossterm::event::KeyEventKind::Release,
    );
    assert_eq!(
        encode_key(key, KeyboardProtocol::Kitty { flags: 7 }),
        b"\x1b[108:76;2:3u"
    );
}

#[test]
fn kitty_shifted_punctuation_literals_send_text() {
    for ch in "!@#$%^&*()_+{}|:\"<>?~".chars() {
        let key = TerminalKey::new(KeyCode::Char(ch), KeyModifiers::SHIFT);
        let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
        assert_eq!(encoded, ch.to_string().into_bytes(), "ch={ch}");
    }
}

#[test]
fn kitty_shifted_punctuation_release_does_not_emit_text() {
    let key = TerminalKey::new(KeyCode::Char('?'), KeyModifiers::SHIFT)
        .with_kind(crossterm::event::KeyEventKind::Release);
    assert_eq!(
        encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 }),
        b"\x1b[63;2:3u"
    );
}

#[test]
fn kitty_shifted_punctuation_does_not_infer_layout() {
    let key = TerminalKey::new(KeyCode::Char('1'), KeyModifiers::SHIFT);
    assert_eq!(
        encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 }),
        b"\x1b[49;2:1u"
    );
}

#[test]
fn kitty_modified_shifted_punctuation_stays_modified_key() {
    for (modifiers, expected) in [
        (
            KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            b"\x1b[33;6:1u".as_slice(),
        ),
        (
            KeyModifiers::ALT | KeyModifiers::SHIFT,
            b"\x1b[33;4:1u".as_slice(),
        ),
        (
            KeyModifiers::SUPER | KeyModifiers::SHIFT,
            b"\x1b[33;10:1u".as_slice(),
        ),
    ] {
        let key = TerminalKey::new(KeyCode::Char('!'), modifiers);
        let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
        assert_eq!(encoded, expected, "modifiers={modifiers:?}");
    }
}

#[test]
fn release_bytes_gated_on_report_event_types() {
    for code in [KeyCode::Enter, KeyCode::Backspace] {
        let release = KeyEvent::new_with_kind(
            code,
            KeyModifiers::empty(),
            crossterm::event::KeyEventKind::Release,
        );

        // Legacy and Kitty disambiguate-only (no REPORT_EVENT_TYPES) must not
        // emit a byte on release, otherwise Enter/Backspace double (issue #769).
        assert_eq!(encode_key(release, KeyboardProtocol::Legacy), b"");
        assert_eq!(
            encode_key(release, KeyboardProtocol::Kitty { flags: 1 }),
            b""
        );
    }

    let modified_release = KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::CONTROL,
        crossterm::event::KeyEventKind::Release,
    );
    assert_eq!(
        encode_key(modified_release, KeyboardProtocol::Kitty { flags: 3 }),
        b"\x1b[13;5:3u"
    );
}

#[test]
fn kitty_shifted_symbol_sends_text() {
    let key = TerminalKey::new(KeyCode::Char('1'), KeyModifiers::SHIFT)
        .with_shifted_codepoint('!' as u32);
    assert_eq!(
        encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 }),
        b"!"
    );
}

#[test]
fn legacy_modified_special_roundtrip_matrix() {
    let cases = [
        KeyEvent::new(KeyCode::Up, KeyModifiers::ALT),
        KeyEvent::new(KeyCode::Down, KeyModifiers::ALT),
        KeyEvent::new(KeyCode::Right, KeyModifiers::SHIFT),
        KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL),
        KeyEvent::new(KeyCode::Home, KeyModifiers::CONTROL),
        KeyEvent::new(KeyCode::End, KeyModifiers::CONTROL | KeyModifiers::SHIFT),
        KeyEvent::new(KeyCode::PageUp, KeyModifiers::ALT),
        KeyEvent::new(KeyCode::PageDown, KeyModifiers::CONTROL),
        KeyEvent::new(KeyCode::Insert, KeyModifiers::SHIFT),
        KeyEvent::new(KeyCode::Delete, KeyModifiers::ALT),
    ];

    for key in cases {
        let encoded = encode_key(key, KeyboardProtocol::Legacy);
        let parsed = parse_terminal_key_sequence(std::str::from_utf8(&encoded).unwrap()).unwrap();
        assert_terminal_key_eq(parsed, key.code, key.modifiers, key.kind, None);
    }
}

#[test]
fn kitty_shifted_symbol_prefers_text_over_roundtrip_key_identity() {
    let key = TerminalKey::new(KeyCode::Char('1'), KeyModifiers::SHIFT)
        .with_shifted_codepoint('!' as u32);
    let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
    assert_eq!(encoded, b"!");
}

#[test]
fn legacy_basic_special_roundtrip_matrix() {
    let cases = [
        KeyEvent::new(KeyCode::Enter, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Tab, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Backspace, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Esc, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Up, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Down, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Left, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Right, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Home, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::End, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::PageUp, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::PageDown, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Insert, KeyModifiers::empty()),
        KeyEvent::new(KeyCode::Delete, KeyModifiers::empty()),
    ];

    for key in cases {
        let encoded = encode_key(key, KeyboardProtocol::Legacy);
        let parsed = parse_terminal_key_sequence(std::str::from_utf8(&encoded).unwrap()).unwrap();
        assert_terminal_key_eq(parsed, key.code, key.modifiers, key.kind, None);
    }
}

#[test]
fn kitty_shifted_symbol_pair_matrix_is_encoded_as_text() {
    let cases = [('1', '!'), ('/', '?'), ('[', '{')];

    for (base, shifted) in cases {
        let key = TerminalKey::new(KeyCode::Char(base), KeyModifiers::SHIFT)
            .with_shifted_codepoint(shifted as u32);
        let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
        assert_eq!(encoded, shifted.to_string().into_bytes(), "base={base}");
    }
}

#[test]
fn chinese_char_encodes_as_utf8() {
    let key = TerminalKey::new(KeyCode::Char('中'), KeyModifiers::empty());
    let encoded = encode_terminal_key(key, KeyboardProtocol::Legacy);
    assert_eq!(encoded, "中".as_bytes());
}

#[test]
fn chinese_char_with_kitty_protocol_encodes_as_utf8() {
    let key = TerminalKey::new(KeyCode::Char('文'), KeyModifiers::empty());
    let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
    assert_eq!(encoded, "文".as_bytes());
}

#[test]
fn chinese_char_with_modifiers_falls_back_to_kitty_encoding() {
    let key = TerminalKey::new(KeyCode::Char('测'), KeyModifiers::ALT);
    let encoded = encode_terminal_key(key, KeyboardProtocol::Kitty { flags: 7 });
    assert!(!encoded.is_empty());
    assert_ne!(encoded, "测".as_bytes());
}
