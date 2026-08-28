use super::*;
use crossterm::event::{KeyCode, KeyEventKind};

fn assert_raw_key(event: RawInputEvent, code: KeyCode, modifiers: KeyModifiers) {
    let RawInputEvent::Key(key) = event else {
        panic!("expected key");
    };
    assert_eq!(key.code, code);
    assert_eq!(key.modifiers, modifiers);
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

fn collect_events(rx: &mut mpsc::Receiver<RawInputEvent>) -> Vec<RawInputEvent> {
    let mut events = Vec::new();
    while let Ok(event) = rx.try_recv() {
        events.push(event);
    }
    events
}

fn drain_chunk(buffer: &mut Vec<u8>, tx: &mpsc::Sender<RawInputEvent>, chunk: &[u8]) {
    buffer.extend_from_slice(chunk);
    drain_buffer(buffer, tx);
}

#[test]
fn parses_kitty_shift_letter_release() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[108:76;2:3u").unwrap()
    else {
        panic!("expected key");
    };
    assert_eq!(consumed, 13);
    assert_eq!(key.code, KeyCode::Char('l'));
    assert_eq!(key.modifiers, KeyModifiers::SHIFT);
    assert_eq!(key.kind, KeyEventKind::Release);
    assert_eq!(key.shifted_codepoint, Some('L' as u32));
}

#[test]
fn parses_bracketed_paste() {
    let (RawInputEvent::Paste(text), consumed) =
        extract_one_event(b"\x1b[200~hello\x1b[201~rest").unwrap()
    else {
        panic!("expected paste");
    };
    assert_eq!(text, "hello");
    assert_eq!(consumed, 17);
}

#[test]
fn complete_text_bracketed_paste_requires_one_exact_utf8_sequence() {
    assert!(is_complete_text_bracketed_paste(b"\x1b[200~hello\x1b[201~"));
    assert!(!is_complete_text_bracketed_paste(b"\x1b[200~hello"));
    assert!(!is_complete_text_bracketed_paste(
        b"\x1b[200~hello\x1b[201~rest"
    ));
    assert!(!is_complete_text_bracketed_paste(
        b"\x1b[200~one\x1b[201~\x1b[200~two\x1b[201~"
    ));
    assert!(!is_complete_text_bracketed_paste(b"\x1b[200~\xff\x1b[201~"));
}

#[test]
fn parses_sgr_mouse() {
    let (RawInputEvent::Mouse(mouse), consumed) = extract_one_event(b"\x1b[<0;20;10M").unwrap()
    else {
        panic!("expected mouse");
    };
    assert_eq!(consumed, 11);
    assert_eq!(mouse.kind, MouseEventKind::Down(MouseButton::Left));
    assert_eq!(mouse.column, 19);
    assert_eq!(mouse.row, 9);
    assert_eq!(mouse.modifiers, KeyModifiers::empty());
}

#[test]
fn parses_extended_button_drag_as_mouse_motion() {
    for input in [
        b"\x1b[<160;20;10M".as_slice(),
        b"\x1b[<161;20;10M".as_slice(),
    ] {
        let (RawInputEvent::Mouse(mouse), _) = extract_one_event(input).unwrap() else {
            panic!("expected mouse");
        };
        assert_eq!(mouse.kind, MouseEventKind::Moved);
        assert_eq!((mouse.column, mouse.row), (19, 9));
    }
}

#[test]
fn parses_sgr_mouse_observable_modifiers() {
    let cases = [
        (b"\x1b[<8;20;10M".as_slice(), KeyModifiers::ALT),
        (b"\x1b[<16;20;10M".as_slice(), KeyModifiers::CONTROL),
        (
            b"\x1b[<24;20;10M".as_slice(),
            KeyModifiers::ALT | KeyModifiers::CONTROL,
        ),
    ];

    for (input, expected) in cases {
        let (RawInputEvent::Mouse(mouse), _) = extract_one_event(input).unwrap() else {
            panic!("expected mouse");
        };
        assert_eq!(mouse.modifiers, expected);
        assert!(!mouse.modifiers.contains(KeyModifiers::SUPER));
    }
}

#[test]
fn parses_host_default_color_response_with_st() {
    let (RawInputEvent::HostDefaultColor { kind, color }, consumed) =
        extract_one_event(b"\x1b]10;rgb:cccc/dddd/eeee\x1b\\").unwrap()
    else {
        panic!("expected host color response");
    };
    assert_eq!(consumed, 25);
    assert_eq!(kind, DefaultColorKind::Foreground);
    assert_eq!(
        color,
        RgbColor {
            r: 0xcc,
            g: 0xdd,
            b: 0xee
        }
    );
}

#[test]
fn parses_host_default_color_response_with_bel() {
    let (RawInputEvent::HostDefaultColor { kind, color }, consumed) =
        extract_one_event(b"\x1b]11;#112233\x07").unwrap()
    else {
        panic!("expected host color response");
    };
    assert_eq!(consumed, 13);
    assert_eq!(kind, DefaultColorKind::Background);
    assert_eq!(
        color,
        RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33
        }
    );
}

#[test]
fn parses_host_palette_color_response() {
    let (RawInputEvent::HostPaletteColors { colors }, consumed) =
        extract_one_event(b"\x1b]4;7;rgb:1111/2222/3333\x1b\\").unwrap()
    else {
        panic!("expected host palette response");
    };
    assert_eq!(consumed, 26);
    assert_eq!(
        colors,
        vec![(
            7,
            RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            }
        )]
    );
}

#[test]
fn parses_legacy_up_arrow() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[A").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 3);
    assert_eq!(key.code, KeyCode::Up);
}

#[test]
fn parses_outer_focus_events() {
    let (event, consumed) = extract_one_event(b"\x1b[I").unwrap();
    assert_eq!(consumed, 3);
    assert!(matches!(event, RawInputEvent::OuterFocusGained));

    let (event, consumed) = extract_one_event(b"\x1b[O").unwrap();
    assert_eq!(consumed, 3);
    assert!(matches!(event, RawInputEvent::OuterFocusLost));
}

#[test]
fn outer_focus_gained_requests_host_surface_redraw() {
    let events = parse_raw_input_bytes_sync(b"\x1b[I");
    assert!(events_require_host_surface_redraw(&events, true));
    assert!(!events_require_host_surface_redraw(&events, false));

    let events = parse_raw_input_bytes_sync(b"\x1b[O");
    assert!(!events_require_host_surface_redraw(&events, true));
}

#[test]
fn parses_ghostty_color_scheme_reports() {
    for bytes in [
        GHOSTTY_COLOR_SCHEME_DARK_REPORT,
        GHOSTTY_COLOR_SCHEME_LIGHT_REPORT,
    ] {
        let events = parse_raw_input_bytes_sync(bytes);
        assert_eq!(events.len(), 1, "bytes: {bytes:?}");
        assert!(matches!(
            events[0],
            RawInputEvent::HostColorSchemeChanged(HostAppearance::Dark | HostAppearance::Light)
        ));
        assert!(events_require_host_terminal_theme_query(&events));
    }
}

#[test]
fn ghostty_color_scheme_report_parser_is_exact() {
    for bytes in [
        b"\x1b[?997;0n".as_slice(),
        b"\x1b[?997;3n".as_slice(),
        b"\x1b[?998;1n".as_slice(),
    ] {
        let events = parse_raw_input_bytes_sync(bytes);
        assert_eq!(events.len(), 1, "bytes: {bytes:?}");
        assert!(matches!(events[0], RawInputEvent::Unsupported));
        assert!(!events_require_host_terminal_theme_query(&events));
    }
}

#[test]
fn raw_input_framer_reassembles_split_color_scheme_report() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b[?997;").is_empty());
    let events = framer.push(b"1n");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::HostColorSchemeChanged(HostAppearance::Dark)
    ));
}

#[test]
fn parses_host_cell_size_report() {
    let events = parse_raw_input_bytes_sync(b"\x1b[6;21;10t");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::HostCellSizeReport {
            width_px: 10,
            height_px: 21,
        }
    ));
}

#[test]
fn host_cell_size_report_parser_is_exact() {
    for bytes in [
        // Zero dimensions carry no usable cell size.
        b"\x1b[6;0;10t".as_slice(),
        b"\x1b[6;21;0t".as_slice(),
        // Missing or extra parameters.
        b"\x1b[6;21t".as_slice(),
        b"\x1b[6;21;10;3t".as_slice(),
        // Other XTWINOPS reports must not be mistaken for a cell size.
        b"\x1b[4;1610;777t".as_slice(),
        b"\x1b[8;37;161t".as_slice(),
        // Non-numeric parameters.
        b"\x1b[6;21;1-t".as_slice(),
    ] {
        assert!(
            parse_host_cell_size_report(bytes).is_none(),
            "bytes: {bytes:?}"
        );
    }
}

#[test]
fn split_color_scheme_timeout_does_not_swallow_legacy_alt_bracket() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b[").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b[".to_vec()]);
}

#[test]
fn raw_input_byte_framer_discards_timed_out_split_color_scheme_report_tail() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b[?997;").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(b"1n").is_empty());
    assert_eq!(framer.push(b"a"), vec![b"a".to_vec()]);
    assert!(framer.flush_timeout().is_empty());
}

#[test]
fn parses_xterm_alt_up_arrow() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[1;3A").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 6);
    assert_eq!(key.code, KeyCode::Up);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parses_legacy_alt_backspace() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b\x7f").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 2);
    assert_eq!(key.code, KeyCode::Backspace);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parses_kitty_alt_backspace() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[127;3u").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 8);
    assert_eq!(key.code, KeyCode::Backspace);
    assert_eq!(key.modifiers, KeyModifiers::ALT);
}

#[test]
fn parses_enhanced_pageup_press() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[5;1:1~").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 8);
    assert_eq!(key.code, KeyCode::PageUp);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, KeyEventKind::Press);
}

#[test]
fn parses_enhanced_pagedown_release() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x1b[6;1:3~").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 8);
    assert_eq!(key.code, KeyCode::PageDown);
    assert_eq!(key.modifiers, KeyModifiers::empty());
    assert_eq!(key.kind, KeyEventKind::Release);
}

#[test]
fn raw_input_family_matrix_is_covered() {
    let cases: &[(&[u8], KeyCode, KeyModifiers)] = &[
        (b"\x02", KeyCode::Char('b'), KeyModifiers::CONTROL),
        (b"\r", KeyCode::Enter, KeyModifiers::empty()),
        (b"\t", KeyCode::Tab, KeyModifiers::empty()),
        (b"\x7f", KeyCode::Backspace, KeyModifiers::empty()),
        (b"\x1b[A", KeyCode::Up, KeyModifiers::empty()),
        (b"\x1b[1;3A", KeyCode::Up, KeyModifiers::ALT),
        (b"\x1b\x7f", KeyCode::Backspace, KeyModifiers::ALT),
        (b"\x1b[127;3u", KeyCode::Backspace, KeyModifiers::ALT),
        (b"\x1b[57420;1u", KeyCode::Down, KeyModifiers::empty()),
        (b"\x1b[57423;1u", KeyCode::Home, KeyModifiers::empty()),
        (b"\x1bOq", KeyCode::Char('1'), KeyModifiers::empty()),
        (b"\x1b[14~", KeyCode::F(4), KeyModifiers::empty()),
        (b"\x1b[49:33;2:1u", KeyCode::Char('1'), KeyModifiers::SHIFT),
    ];

    for (bytes, code, modifiers) in cases {
        let (event, consumed) = extract_one_event(bytes).unwrap();
        assert_eq!(consumed, bytes.len());
        assert_raw_key(event, *code, *modifiers);
    }
}

#[test]
fn raw_framer_waits_for_application_keypad_sequence_final_byte() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1bO").is_empty());
    let events = framer.push(b"q");

    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('1'),
        KeyModifiers::empty(),
    );
}

#[test]
fn unsupported_ss3_sequence_stays_unsupported() {
    let (event, consumed) = extract_one_event(b"\x1bOz").unwrap();

    assert_eq!(consumed, 3);
    assert!(matches!(event, RawInputEvent::Unsupported));
}

#[test]
fn modified_rxvt_f_key_alias_stays_unsupported() {
    let (event, consumed) = extract_one_event(b"\x1b[14;3~").unwrap();

    assert_eq!(consumed, 7);
    assert!(matches!(event, RawInputEvent::Unsupported));
}

#[test]
fn flushes_lone_escape_after_timeout() {
    let (tx, mut rx) = mpsc::channel(4);
    let mut buffer = vec![ESC];
    flush_incomplete_buffer(&mut buffer, &tx);
    assert!(buffer.is_empty());
    let event = rx.try_recv().unwrap();
    let RawInputEvent::Key(key) = event else {
        panic!("expected key");
    };
    assert_eq!(key.code, KeyCode::Esc);
}

#[test]
fn parses_raw_ctrl_b() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\x02").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 1);
    assert_eq!(key.code, KeyCode::Char('b'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL);
}

#[test]
fn parses_raw_lf_as_ctrl_j() {
    let (RawInputEvent::Key(key), consumed) = extract_one_event(b"\n").unwrap() else {
        panic!("expected key");
    };
    assert_eq!(consumed, 1);
    assert_eq!(key.code, KeyCode::Char('j'));
    assert_eq!(key.modifiers, KeyModifiers::CONTROL);
}

fn assert_fixture_extracts_whole_events(corpus: &str, macos_layout: bool) {
    for line in corpus.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }

        let mut columns: Vec<_> = line.split('\t').collect();
        if columns.len() == 5 {
            columns.push("");
        }

        if macos_layout {
            if columns.len() == 6 {
                columns.push("");
            }
            assert_eq!(
                columns.len(),
                7,
                "macOS fixture row must have 7 columns: {line}"
            );
            if columns[2].is_empty() {
                continue;
            }
            let bytes = decode_hex(columns[2]);
            let (event, consumed) = extract_one_event(&bytes).unwrap();
            assert_eq!(
                consumed,
                bytes.len(),
                "fixture should extract a whole event: {line}"
            );
            assert_raw_key(
                event,
                parse_fixture_key_code(columns[3]),
                parse_fixture_modifiers(columns[4]),
            );
        } else {
            if columns.len() == 5 {
                columns.push("");
            }
            let (bytes_hex, code, modifiers) = match columns.len() {
                6 => {
                    if columns[1].chars().all(|ch| ch.is_ascii_hexdigit()) {
                        (columns[1], columns[2], columns[3])
                    } else {
                        (columns[2], columns[3], columns[4])
                    }
                }
                7 => (columns[2], columns[3], columns[4]),
                _ => panic!("fixture row must have 6 or 7 columns: {line}"),
            };
            assert!(
                bytes_hex.chars().all(|ch| ch.is_ascii_hexdigit()),
                "non-hex fixture bytes: {bytes_hex} in {line}"
            );
            let bytes = decode_hex(bytes_hex);
            let (event, consumed) = extract_one_event(&bytes).unwrap();
            assert_eq!(
                consumed,
                bytes.len(),
                "fixture should extract a whole event: {line}"
            );
            assert_raw_key(
                event,
                parse_fixture_key_code(code),
                parse_fixture_modifiers(modifiers),
            );
        }
    }
}

#[test]
fn raw_input_corpus_fixture_extracts_whole_events() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/keyboard_protocol_corpus.tsv"
    ));
    assert_fixture_extracts_whole_events(corpus, false);
}

#[test]
fn raw_input_macos_terminal_variants_fixture_extracts_whole_events() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/macos_terminal_variants.tsv"
    ));
    assert_fixture_extracts_whole_events(corpus, true);
}

#[test]
fn raw_input_linux_terminal_variants_fixture_extracts_whole_events() {
    let corpus = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/linux_terminal_variants.tsv"
    ));
    assert_fixture_extracts_whole_events(corpus, false);
}

#[test]
fn chunked_legacy_arrow_waits_for_completion() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b");
    assert_eq!(buffer, b"\x1b");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"[A");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Up,
        KeyModifiers::empty(),
    );
}

#[test]
fn lone_escape_is_buffered_until_timeout_flush() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b");
    assert_eq!(buffer, b"\x1b");
    assert!(collect_events(&mut rx).is_empty());

    flush_incomplete_buffer(&mut buffer, &tx);
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Esc,
        KeyModifiers::empty(),
    );
}

#[test]
fn escape_followed_by_arrow_before_flush_does_not_emit_escape() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b");
    assert_eq!(buffer, b"\x1b");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"[B");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Down,
        KeyModifiers::empty(),
    );
}

#[test]
fn escape_followed_by_sgr_mouse_before_flush_does_not_emit_text() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    let events = framer.push(b"[<65;43;26M");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::Mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: 42,
            row: 25,
            ..
        })
    ));
}

#[test]
fn lone_escape_then_complete_sgr_mouse_report_emits_both_events() {
    for report in [b"\x1b[<35;10;20M".as_slice(), b"\x1b[<35;10;20m".as_slice()] {
        let mut framer = RawInputFramer::default();

        assert!(framer.push(b"\x1b").is_empty());
        let events = framer.push(report);

        assert_eq!(events.len(), 2);
        let mut events = events.into_iter();
        assert_raw_key(events.next().unwrap(), KeyCode::Esc, KeyModifiers::empty());
        assert!(matches!(
            events.next().unwrap(),
            RawInputEvent::Mouse(MouseEvent {
                kind: MouseEventKind::Moved,
                column: 9,
                row: 19,
                ..
            })
        ));
        assert!(framer.flush_timeout().is_empty());
    }
}

#[test]
fn legacy_doubled_escape_alt_arrow_remains_one_event() {
    let mut framer = RawInputFramer::default();

    let events = framer.push(b"\x1b\x1b[A");

    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Up,
        KeyModifiers::ALT,
    );
    assert!(framer.flush_timeout().is_empty());
}

#[cfg(not(target_os = "macos"))]
#[test]
fn non_macos_host_input_splits_lone_escape_from_arrow() {
    let mut framer = RawInputByteFramer::for_host_input();

    assert_eq!(
        framer.push(b"\x1b\x1b[D"),
        vec![b"\x1b".to_vec(), b"\x1b[D".to_vec()]
    );
}

#[test]
fn macos_host_input_policy_preserves_legacy_doubled_escape_alt_arrow() {
    let mut framer = RawInputByteFramer::with_host_input_policy(true);

    assert_eq!(framer.push(b"\x1b\x1b[D"), vec![b"\x1b\x1b[D".to_vec()]);
}

#[test]
fn legacy_reader_extends_only_incomplete_sgr_mouse_timeout() {
    let mut mouse = RawInputFramer::default();
    assert!(mouse.push(b"\x1b[<3").is_empty());
    assert_eq!(
        input_flush_timeout_ms(&mouse),
        MOUSE_ACTIVE_ESCAPE_SEQUENCE_FLUSH_TIMEOUT_MS
    );

    let mut escape = RawInputFramer::default();
    assert!(escape.push(b"\x1b").is_empty());
    assert_eq!(
        input_flush_timeout_ms(&escape),
        RAW_INPUT_IDLE_FLUSH_TIMEOUT_MS
    );
}

#[test]
fn sgr_mouse_sequence_split_after_button_prefix_is_reassembled_before_timeout() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b[<3").is_empty());
    let events = framer.push(b"5;58;30M");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::Mouse(MouseEvent {
            kind: MouseEventKind::Moved,
            column: 57,
            row: 29,
            ..
        })
    ));
}

#[test]
fn timed_out_split_sgr_mouse_tail_is_discarded_and_following_input_is_preserved() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b[<3").is_empty());
    assert!(framer.flush_timeout().is_empty());
    let events = framer.push(b"5;58;30Mx");

    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('x'),
        KeyModifiers::empty(),
    );
}

#[test]
fn timed_out_sgr_mouse_discard_state_clears_at_quiescence() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b[<3").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert_eq!(framer.push(b"M"), vec![b"M".to_vec()]);
}

#[test]
fn sgr_mouse_tail_after_lone_escape_timeout_is_discarded() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    let timeout_events = framer.flush_timeout();
    assert_eq!(timeout_events.len(), 1);
    assert_raw_key(
        timeout_events.into_iter().next().unwrap(),
        KeyCode::Esc,
        KeyModifiers::empty(),
    );

    assert!(framer.push(b"[<65;43;26M").is_empty());
}

#[test]
fn input_after_discarded_complete_sgr_mouse_tail_is_preserved() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout().len(), 1);
    let events = framer.push(b"[<65;43;26Mx");
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('x'),
        KeyModifiers::empty(),
    );
}

#[test]
fn invalid_orphaned_sgr_mouse_tail_after_escape_timeout_is_preserved() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout().len(), 1);

    let events = framer.push(b"[<x");

    assert_eq!(events.len(), 3);
    assert_raw_key(
        events.into_iter().last().unwrap(),
        KeyCode::Char('x'),
        KeyModifiers::empty(),
    );
}

#[test]
fn double_split_sgr_mouse_tail_after_lone_escape_timeout_is_discarded() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout().len(), 1);

    assert!(framer.push(b"[<65;4").is_empty());
    assert!(framer.flush_timeout().is_empty());
    let events = framer.push(b"3;26Mx");
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('x'),
        KeyModifiers::empty(),
    );
}

#[test]
fn escape_followed_by_alt_char_before_flush_becomes_alt_key() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b");
    assert_eq!(buffer, b"\x1b");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"b");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('b'),
        KeyModifiers::ALT,
    );
}

#[test]
fn chunked_kitty_sequence_waits_for_completion() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b[49:33;2:");
    assert_eq!(buffer, b"\x1b[49:33;2:");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"1u");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('1'),
        KeyModifiers::SHIFT,
    );
}

#[test]
fn chunked_bracketed_paste_waits_for_terminator() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b[200~hello");
    assert_eq!(buffer, b"\x1b[200~hello");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"\x1b[201~");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    let RawInputEvent::Paste(text) = &events[0] else {
        panic!("expected paste");
    };
    assert_eq!(text, "hello");
}

#[test]
fn incomplete_bracketed_paste_is_not_flushed_on_timeout() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, b"\x1b[200~hello\nworld");
    assert_eq!(buffer, b"\x1b[200~hello\nworld");
    assert!(collect_events(&mut rx).is_empty());

    flush_incomplete_buffer(&mut buffer, &tx);
    assert_eq!(buffer, b"\x1b[200~hello\nworld");
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, b"\x1b[201~");
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    let RawInputEvent::Paste(text) = &events[0] else {
        panic!("expected paste");
    };
    assert_eq!(text, "hello\nworld");
}

#[test]
fn complete_utf8_char_before_incomplete_char_is_drained() {
    let mut buffer = "你".as_bytes().to_vec();
    buffer.push("好".as_bytes()[0]);

    let chunks = drain_complete_input_bytes(&mut buffer);

    assert_eq!(chunks, vec!["你".as_bytes().to_vec()]);
    assert_eq!(buffer, vec!["好".as_bytes()[0]]);
}

#[test]
fn incomplete_utf8_prefix_is_not_flushed_on_timeout() {
    let mut buffer = vec!["好".as_bytes()[0]];

    assert_eq!(flush_incomplete_input_bytes(&mut buffer), None);
    assert_eq!(buffer, vec!["好".as_bytes()[0]]);
}

#[test]
fn invalid_utf8_lead_byte_is_flushed_instead_of_buffered_forever() {
    let mut buffer = vec![0xC0];

    assert_eq!(flush_incomplete_input_bytes(&mut buffer), None);
    assert!(buffer.is_empty());
}

#[test]
fn complete_utf8_char_before_incomplete_char_survives_timeout_and_next_chunk() {
    let mut buffer = "你".as_bytes().to_vec();
    buffer.push("好".as_bytes()[0]);

    let chunks = drain_complete_input_bytes(&mut buffer);
    assert_eq!(chunks, vec!["你".as_bytes().to_vec()]);
    assert_eq!(flush_incomplete_input_bytes(&mut buffer), None);
    assert_eq!(buffer, vec!["好".as_bytes()[0]]);

    buffer.extend_from_slice(&"好".as_bytes()[1..]);
    let chunks = drain_complete_input_bytes(&mut buffer);
    assert_eq!(chunks, vec!["好".as_bytes().to_vec()]);
    assert!(buffer.is_empty());
}

#[test]
fn alt_utf8_char_drains_as_one_event_before_following_input() {
    let events = parse_raw_input_bytes_sync("\x1béx".as_bytes());
    assert_eq!(events.len(), 2);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('é'),
        KeyModifiers::ALT,
    );
}

#[test]
fn chunked_alt_utf8_waits_for_continuation_byte_after_escape() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();
    let bytes = "\x1bé".as_bytes();

    drain_chunk(&mut buffer, &tx, &bytes[..2]);
    assert_eq!(buffer, bytes[..2]);
    assert!(collect_events(&mut rx).is_empty());
    flush_incomplete_buffer(&mut buffer, &tx);
    assert_eq!(buffer, bytes[..2]);
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, &bytes[2..]);
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('é'),
        KeyModifiers::ALT,
    );
}

#[test]
fn chunked_utf8_waits_for_continuation_byte() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();

    drain_chunk(&mut buffer, &tx, "é".as_bytes().get(..1).unwrap());
    assert_eq!(buffer, vec![0xC3]);
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, "é".as_bytes().get(1..).unwrap());
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('é'),
        KeyModifiers::empty(),
    );
}

#[test]
fn chunked_cjk_utf8_waits_for_all_continuation_bytes() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();
    let bytes = "好".as_bytes();

    drain_chunk(&mut buffer, &tx, &bytes[..1]);
    assert_eq!(buffer, bytes[..1]);
    assert!(collect_events(&mut rx).is_empty());
    flush_incomplete_buffer(&mut buffer, &tx);
    assert_eq!(buffer, bytes[..1]);
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, &bytes[1..2]);
    assert_eq!(buffer, bytes[..2]);
    assert!(collect_events(&mut rx).is_empty());
    flush_incomplete_buffer(&mut buffer, &tx);
    assert_eq!(buffer, bytes[..2]);
    assert!(collect_events(&mut rx).is_empty());

    drain_chunk(&mut buffer, &tx, &bytes[2..]);
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('好'),
        KeyModifiers::empty(),
    );
}

#[test]
fn chunked_four_byte_utf8_waits_for_all_continuation_bytes() {
    let (tx, mut rx) = mpsc::channel(8);
    let mut buffer = Vec::new();
    let bytes = "🙂".as_bytes();

    for split in 1..bytes.len() {
        drain_chunk(&mut buffer, &tx, &bytes[split - 1..split]);
        assert_eq!(buffer, bytes[..split]);
        assert!(collect_events(&mut rx).is_empty());
        flush_incomplete_buffer(&mut buffer, &tx);
        assert_eq!(buffer, bytes[..split]);
        assert!(collect_events(&mut rx).is_empty());
    }

    drain_chunk(&mut buffer, &tx, &bytes[bytes.len() - 1..]);
    assert!(buffer.is_empty());
    let events = collect_events(&mut rx);
    assert_eq!(events.len(), 1);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('🙂'),
        KeyModifiers::empty(),
    );
}

#[test]
fn long_multilingual_voice_like_burst_drains_without_truncation() {
    let text = "你好，今天我们测试一段比较长的语音输入。こんにちは。안녕하세요.🙂".repeat(128);
    assert!(
        text.len() > 4096,
        "test input should exceed the client read buffer"
    );
    let mut buffer = text.as_bytes().to_vec();

    let chunks = drain_complete_input_bytes(&mut buffer);
    let rebuilt: Vec<u8> = chunks.into_iter().flatten().collect();

    assert!(buffer.is_empty());
    assert_eq!(rebuilt, text.as_bytes());
}

#[test]
fn long_multilingual_burst_survives_one_byte_chunks_and_timeouts() {
    let text = "中文かなカナ한글🙂，。".repeat(64);
    let mut buffer = Vec::new();
    let mut rebuilt = Vec::new();

    for byte in text.as_bytes() {
        buffer.push(*byte);
        for chunk in drain_complete_input_bytes(&mut buffer) {
            rebuilt.extend(chunk);
        }
        if !buffer.is_empty() {
            assert_eq!(flush_incomplete_input_bytes(&mut buffer), None);
        }
    }

    for chunk in drain_complete_input_bytes(&mut buffer) {
        rebuilt.extend(chunk);
    }

    assert!(buffer.is_empty());
    assert_eq!(rebuilt, text.as_bytes());
}

#[test]
fn parse_with_ranges_tracks_byte_offsets() {
    use super::parse_raw_input_bytes_with_ranges;

    // Input: Up arrow (3 bytes) + 'a' (1 byte) + Down arrow (3 bytes)
    let input = b"\x1b[Aa\x1b[B".to_vec();
    let ranges = parse_raw_input_bytes_with_ranges(&input);

    assert_eq!(ranges.len(), 3, "should parse three events");

    // Up arrow: \x1b[A at offset 0, length 3
    assert_eq!(ranges[0].start, 0);
    assert_eq!(ranges[0].len, 3);
    assert!(matches!(
        &ranges[0].event,
        RawInputEvent::Key(k) if k.code == KeyCode::Up
    ));

    // 'a' at offset 3, length 1
    assert_eq!(ranges[1].start, 3);
    assert_eq!(ranges[1].len, 1);
    assert!(matches!(
        &ranges[1].event,
        RawInputEvent::Key(k) if k.code == KeyCode::Char('a')
    ));

    // Down arrow: \x1b[B at offset 4, length 3
    assert_eq!(ranges[2].start, 4);
    assert_eq!(ranges[2].len, 3);
    assert!(matches!(
        &ranges[2].event,
        RawInputEvent::Key(k) if k.code == KeyCode::Down
    ));

    // Verify the raw bytes for each event slice correctly.
    assert_eq!(
        &input[ranges[0].start..ranges[0].start + ranges[0].len],
        b"\x1b[A"
    );
    assert_eq!(
        &input[ranges[1].start..ranges[1].start + ranges[1].len],
        b"a"
    );
    assert_eq!(
        &input[ranges[2].start..ranges[2].start + ranges[2].len],
        b"\x1b[B"
    );
}

#[test]
fn parse_with_ranges_handles_single_event() {
    use super::parse_raw_input_bytes_with_ranges;

    let input = b"a".to_vec();
    let ranges = parse_raw_input_bytes_with_ranges(&input);

    assert_eq!(ranges.len(), 1);
    assert_eq!(ranges[0].start, 0);
    assert_eq!(ranges[0].len, 1);
}

#[test]
fn parse_with_ranges_handles_mouse_event() {
    use super::parse_raw_input_bytes_with_ranges;

    let input = b"\x1b[<0;20;10M".to_vec();
    let ranges = parse_raw_input_bytes_with_ranges(&input);

    assert_eq!(ranges.len(), 1);
    assert_eq!(ranges[0].start, 0);
    assert_eq!(ranges[0].len, input.len());
    assert!(matches!(&ranges[0].event, RawInputEvent::Mouse(_)));
}

#[test]
fn parses_ghostty_default_background_response() {
    let events = parse_raw_input_bytes_sync(b"\x1b]11;rgb:2828/2a2a/3636\x07");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::HostDefaultColor {
            kind: DefaultColorKind::Background,
            color: RgbColor {
                r: 0x28,
                g: 0x2a,
                b: 0x36
            }
        }
    ));
}

#[test]
fn drain_complete_input_bytes_keeps_split_default_background_response_buffered() {
    let mut buffer = b"\x1b]11;rgb:2828".to_vec();

    let chunks = drain_complete_input_bytes(&mut buffer);

    assert!(chunks.is_empty());
    assert_eq!(buffer, b"\x1b]11;rgb:2828");
}

#[test]
fn flush_incomplete_input_bytes_keeps_split_default_background_response_buffered() {
    let mut buffer = b"\x1b]11;rgb:2828".to_vec();

    let flushed = flush_incomplete_input_bytes(&mut buffer);

    assert!(flushed.is_none());
    assert_eq!(buffer, b"\x1b]11;rgb:2828");
}

#[test]
fn flush_incomplete_input_bytes_keeps_default_background_response_split_after_command() {
    let mut buffer = b"\x1b]11;".to_vec();

    let flushed = flush_incomplete_input_bytes(&mut buffer);

    assert!(flushed.is_none());
    assert_eq!(buffer, b"\x1b]11;");
}

#[test]
fn flush_incomplete_input_bytes_keeps_default_background_response_split_inside_st() {
    let mut buffer = b"\x1b]11;rgb:2828/2a2a/3636\x1b".to_vec();

    let flushed = flush_incomplete_input_bytes(&mut buffer);

    assert!(flushed.is_none());
    assert_eq!(buffer, b"\x1b]11;rgb:2828/2a2a/3636\x1b");
}

#[test]
fn drain_complete_input_bytes_keeps_bare_osc_introducer_buffered() {
    let mut buffer = b"\x1b]".to_vec();

    let chunks = drain_complete_input_bytes(&mut buffer);

    assert!(chunks.is_empty());
    assert_eq!(buffer, b"\x1b]");
}

#[test]
fn flush_incomplete_input_bytes_drops_bare_osc_introducer_after_timeout() {
    let mut buffer = b"\x1b]".to_vec();

    let flushed = flush_incomplete_input_bytes(&mut buffer);

    assert!(flushed.is_none());
    assert!(buffer.is_empty());
}

#[test]
fn flush_incomplete_input_bytes_drops_string_introducers_after_timeout() {
    for bytes in [
        b"\x1b]".as_slice(),
        b"\x1bP".as_slice(),
        b"\x1b_".as_slice(),
        b"\x1b^".as_slice(),
        b"\x1bX".as_slice(),
    ] {
        let mut buffer = bytes.to_vec();

        let flushed = flush_incomplete_input_bytes(&mut buffer);

        assert!(flushed.is_none(), "flushed {bytes:?}");
        assert!(buffer.is_empty(), "kept {bytes:?}");
    }
}

#[test]
fn raw_input_framer_reassembles_split_default_background_response() {
    let mut framer = RawInputFramer::default();

    assert!(framer.push(b"\x1b]").is_empty());
    let events = framer.push(b"11;#123456\x07");

    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        RawInputEvent::HostDefaultColor {
            kind: DefaultColorKind::Background,
            color: RgbColor {
                r: 0x12,
                g: 0x34,
                b: 0x56,
            }
        }
    ));
}

#[test]
fn raw_input_byte_framer_discards_split_control_string_after_timeout() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b]").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(b"11;#123456\x07").is_empty());
    assert_eq!(framer.push(b"a"), vec![b"a".to_vec()]);
}

#[test]
fn raw_input_byte_framer_keeps_discarding_tail_across_timeout() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b]").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(b"1").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(b"1;#123456\x07").is_empty());
    assert_eq!(framer.push(b"a"), vec![b"a".to_vec()]);
}

#[test]
fn raw_input_byte_framer_releases_discard_on_implausible_tail() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b]").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(b"a").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert_eq!(framer.push(b"b"), vec![b"b".to_vec()]);
}

#[test]
fn parse_raw_input_bytes_sync_does_not_parse_incomplete_strings_as_alt_keys() {
    for bytes in [
        b"\x1b]".as_slice(),
        b"\x1bP".as_slice(),
        b"\x1b_".as_slice(),
        b"\x1b^".as_slice(),
        b"\x1bX".as_slice(),
    ] {
        let events = parse_raw_input_bytes_sync(bytes);

        assert!(events.is_empty(), "parsed {bytes:?} as {events:?}");
    }
}

#[test]
fn non_osc_control_strings_ignore_bel_and_complete_at_st() {
    let bytes = b"\x1bPabc\x07def\x1b\\x";

    let (event, consumed) = extract_one_event(bytes).unwrap();

    assert!(matches!(event, RawInputEvent::Unsupported));
    assert_eq!(consumed, b"\x1bPabc\x07def\x1b\\".len());
}

#[test]
fn non_osc_default_color_text_remains_key_input() {
    let events = parse_raw_input_bytes_sync(b"11;rgb:2828/2a2a/3636\x07");

    assert_eq!(events.len(), 22);
    assert_raw_key(
        events.into_iter().next().unwrap(),
        KeyCode::Char('1'),
        KeyModifiers::empty(),
    );
}

#[test]
fn flush_incomplete_input_bytes_does_not_hold_non_osc_default_color_text() {
    let mut framer = RawInputByteFramer::default();

    let chunks = framer.push(b"11;rgb:2828");

    assert_eq!(chunks.len(), 11);
    assert!(framer.flush_timeout().is_empty());
}

#[test]
fn holds_lone_escape_and_stitches_split_host_color_reply() {
    let mut framer = RawInputByteFramer::default();
    framer.host_color_query_sent();

    // The reply is split right at its ESC introducer.
    assert!(framer.push(b"\x1b").is_empty());
    // The idle flush must not release the ESC as an Escape key while a host
    // color reply is still outstanding.
    assert!(framer.flush_timeout().is_empty());

    // The rest of the OSC 11 reply arrives and stitches back together
    // instead of leaking its payload into the focused pane.
    let chunks = framer.push(b"]11;rgb:2424/2727/3a3a\x1b\\");
    assert_eq!(chunks.len(), 1);
    let (event, _) = extract_one_event(&chunks[0]).unwrap();
    assert!(matches!(
        event,
        RawInputEvent::HostDefaultColor {
            kind: DefaultColorKind::Background,
            ..
        }
    ));
}

#[test]
fn holds_lone_escape_and_stitches_split_host_cell_size_reply() {
    let mut framer = RawInputByteFramer::default();
    framer.host_cell_size_query_sent();

    // The XTWINOPS reply is split right at its ESC introducer.
    assert!(framer.push(b"\x1b").is_empty());
    assert!(framer.flush_timeout().is_empty());

    let chunks = framer.push(b"[6;21;10t");
    assert_eq!(chunks, vec![b"\x1b[6;21;10t".to_vec()]);
    let (event, _) = extract_one_event(&chunks[0]).unwrap();
    assert!(matches!(
        event,
        RawInputEvent::HostCellSizeReport {
            width_px: 10,
            height_px: 21,
        }
    ));
}

#[test]
fn timed_out_host_cell_size_reply_fragments_do_not_leak() {
    for (prefix, tail) in [
        (b"\x1b[6".as_slice(), b";21;10t".as_slice()),
        (b"\x1b[6;".as_slice(), b"21;10t".as_slice()),
        (b"\x1b[6;21;".as_slice(), b"10t".as_slice()),
    ] {
        let mut framer = RawInputByteFramer::default();
        framer.host_cell_size_query_sent();

        assert!(framer.push(prefix).is_empty(), "prefix: {prefix:?}");
        assert!(framer.flush_timeout().is_empty(), "prefix: {prefix:?}");
        assert!(framer.push(tail).is_empty(), "tail: {tail:?}");
        assert_eq!(framer.push(b"a"), vec![b"a".to_vec()]);
    }
}

#[test]
fn split_host_cell_size_reply_after_csi_intro_gets_one_more_flush() {
    let mut framer = RawInputByteFramer::default();
    framer.host_cell_size_query_sent();

    assert!(framer.push(b"\x1b[").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert_eq!(framer.push(b"6;21;10t"), vec![b"\x1b[6;21;10t".to_vec()]);

    let mut alt_bracket = RawInputByteFramer::default();
    alt_bracket.host_cell_size_query_sent();
    assert!(alt_bracket.push(b"\x1b[").is_empty());
    assert!(alt_bracket.flush_timeout().is_empty());
    assert_eq!(alt_bracket.flush_timeout(), vec![b"\x1b[".to_vec()]);
}

#[test]
fn malformed_host_reply_tail_preserves_following_input() {
    let mut framer = RawInputByteFramer::default();
    framer.host_cell_size_query_sent();

    assert!(framer.push(b"\x1b[6;21").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert_eq!(
        framer.push(b";10xabc"),
        vec![b"a".to_vec(), b"b".to_vec(), b"c".to_vec()]
    );
}

#[test]
fn host_reply_tail_discard_is_bounded_across_pushes() {
    let mut framer = RawInputByteFramer::default();
    framer.host_cell_size_query_sent();

    assert!(framer.push(b"\x1b[6;21").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert!(framer.push(&[b'1'; 64]).is_empty());
    assert_eq!(
        framer.push(&[b'2'; 67]),
        vec![b"2".to_vec(), b"2".to_vec(), b"2".to_vec()]
    );
}

#[test]
fn stops_holding_lone_escape_after_host_cell_size_reply_completes() {
    let mut framer = RawInputByteFramer::default();
    framer.host_cell_size_query_sent();

    assert_eq!(
        framer.push(b"\x1b[6;21;10t"),
        vec![b"\x1b[6;21;10t".to_vec()]
    );

    // Window closed: a later lone Escape flushes immediately.
    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn default_byte_framer_does_not_rearm_after_color_scheme_report() {
    let mut framer = RawInputByteFramer::default();

    assert_eq!(
        framer.push(GHOSTTY_COLOR_SCHEME_DARK_REPORT),
        vec![GHOSTTY_COLOR_SCHEME_DARK_REPORT.to_vec()]
    );
    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn opt_in_does_not_delay_plain_escape_without_color_scheme_report() {
    let mut framer = RawInputByteFramer::default();
    framer.enable_host_color_scheme_change_tracking();

    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn opted_in_byte_framer_rearms_after_color_scheme_report() {
    let mut framer = RawInputByteFramer::default();
    framer.enable_host_color_scheme_change_tracking();

    assert_eq!(
        framer.push(GHOSTTY_COLOR_SCHEME_DARK_REPORT),
        vec![GHOSTTY_COLOR_SCHEME_DARK_REPORT.to_vec()]
    );

    assert!(framer.push(b"\x1b").is_empty());
    assert!(framer.flush_timeout().is_empty());
    let chunks = framer.push(b"]10;#abcdef\x07");
    assert_eq!(chunks.len(), 1);
    let (event, _) = extract_one_event(&chunks[0]).unwrap();
    assert!(matches!(
        event,
        RawInputEvent::HostDefaultColor {
            kind: DefaultColorKind::Foreground,
            color: RgbColor {
                r: 0xab,
                g: 0xcd,
                b: 0xef
            }
        }
    ));

    assert!(framer.push(b"\x1b").is_empty());
    assert!(framer.flush_timeout().is_empty());
    let chunks = framer.push(b"]11;#123456\x07");
    assert_eq!(chunks.len(), 1);
    let (event, _) = extract_one_event(&chunks[0]).unwrap();
    assert!(matches!(
        event,
        RawInputEvent::HostDefaultColor {
            kind: DefaultColorKind::Background,
            color: RgbColor {
                r: 0x12,
                g: 0x34,
                b: 0x56
            }
        }
    ));

    assert!(framer.push(b"\x1b").is_empty());
    assert!(framer.flush_timeout().is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn flushes_lone_escape_when_not_awaiting_host_color_reply() {
    let mut framer = RawInputByteFramer::default();

    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn gives_up_holding_lone_escape_after_one_idle_flush() {
    let mut framer = RawInputByteFramer::default();
    framer.host_color_query_sent();

    assert!(framer.push(b"\x1b").is_empty());
    // First idle flush holds the escape.
    assert!(framer.flush_timeout().is_empty());
    // No continuation arrived; the second idle flush releases it as Escape.
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}

#[test]
fn stops_holding_lone_escape_after_host_color_reply_completes() {
    use std::fmt::Write as _;

    let mut framer = RawInputByteFramer::default();
    framer.host_color_query_sent();
    let mut replies =
        String::from("\x1b]10;rgb:6565/7b7b/8383\x1b\\\x1b]11;rgb:2424/2727/3a3a\x1b\\");
    for index in 0..=u8::MAX {
        let _ = write!(replies, "\x1b]4;{index};rgb:1111/2222/3333\x1b\\");
    }

    let chunks = framer.push(replies.as_bytes());
    assert_eq!(chunks.len(), 258);

    // Window closed: a later lone Escape flushes immediately.
    assert!(framer.push(b"\x1b").is_empty());
    assert_eq!(framer.flush_timeout(), vec![b"\x1b".to_vec()]);
}
