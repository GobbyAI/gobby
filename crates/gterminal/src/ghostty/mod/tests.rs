use super::*;

fn write_numbered_lines(terminal: &mut Terminal, count: usize) {
    for i in 0..count {
        terminal.write(format!("{i:06}\r\n").as_bytes());
    }
}

fn write_padded_lines(terminal: &mut Terminal, count: usize, width: usize) {
    let line = format!("{}\r\n", "x".repeat(width));
    terminal.write(line.repeat(count).as_bytes());
}

fn first_rendered_row_text(terminal: &Terminal) -> String {
    let mut render_state = RenderState::new().unwrap();
    render_state.update(terminal).unwrap();
    let mut row_iterator = RowIterator::new().unwrap();
    let mut rows = render_state
        .populate_row_iterator(&mut row_iterator)
        .unwrap();
    let mut row_cells = RowCells::new().unwrap();
    let mut bytes = Vec::new();
    let mut cell_text = String::new();
    let mut row_text = String::new();

    assert!(rows.next());
    let mut cells = rows.populate_cells(&mut row_cells).unwrap();
    while cells.next() {
        cells
            .grapheme_text_into(&mut bytes, &mut cell_text)
            .unwrap();
        row_text.push_str(&cell_text);
    }
    row_text.trim_end().to_owned()
}

fn build_info_bool(data: ffi::GhosttyBuildInfo) -> bool {
    let mut out = false;
    unsafe {
        ffi::ghostty_build_info(data, (&mut out as *mut bool).cast())
            .into_result()
            .unwrap();
    }
    out
}

fn build_info_optimize() -> ffi::GhosttyOptimizeMode {
    let mut out = ffi::GhosttyOptimizeMode_GHOSTTY_OPTIMIZE_DEBUG;
    unsafe {
        ffi::ghostty_build_info(
            ffi::GhosttyBuildInfo_GHOSTTY_BUILD_INFO_OPTIMIZE,
            (&mut out as *mut ffi::GhosttyOptimizeMode).cast(),
        )
        .into_result()
        .unwrap();
    }
    out
}

#[test]
fn kitty_image_fingerprint_covers_full_payload() {
    let mut data = vec![1u8; 4096 * 4];
    let original =
        kitty_image_fingerprint(data.as_ptr(), data.len(), 100, 50, KittyImageFormat::Png);

    data[4096 + 123] = 2;
    let changed_outside_sampled_windows =
        kitty_image_fingerprint(data.as_ptr(), data.len(), 100, 50, KittyImageFormat::Png);
    assert_ne!(original, changed_outside_sampled_windows);
}

#[test]
fn kitty_image_fingerprint_refreshes_on_retransmission() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.write(b"\x1b_Ga=T,f=32,t=d,i=7,p=3,s=1,v=1,c=10,r=5,q=2;/wAA/w==\x1b\\");
    let first = terminal
        .kitty_image_placements_with_data_filter(|_| true)
        .unwrap();
    assert_eq!(first.len(), 1);
    let first_generation = terminal
        .kitty_fingerprints
        .lock()
        .unwrap()
        .get(&7)
        .unwrap()
        .generation;
    assert_ne!(first_generation, 0);

    // Same id and size, different pixels.
    terminal.write(b"\x1b_Ga=t,f=32,t=d,i=7,s=1,v=1,q=2;AAAAAA==\x1b\\");
    let second = terminal
        .kitty_image_placements_with_data_filter(|_| true)
        .unwrap();
    assert_eq!(second.len(), 1);
    assert_ne!(first[0].data_fingerprint, second[0].data_fingerprint);
    let second_generation = terminal
        .kitty_fingerprints
        .lock()
        .unwrap()
        .get(&7)
        .unwrap()
        .generation;
    assert_ne!(first_generation, second_generation);

    // No retransmission, so the fingerprint and generation stay stable.
    let third = terminal
        .kitty_image_placements_with_data_filter(|_| true)
        .unwrap();
    assert_eq!(second[0].data_fingerprint, third[0].data_fingerprint);
    assert_eq!(
        terminal
            .kitty_fingerprints
            .lock()
            .unwrap()
            .get(&7)
            .unwrap()
            .generation,
        second_generation
    );
}

#[test]
fn kitty_storage_generation_skips_only_proven_empty_storage() {
    let mut terminal = Terminal::new(10, 5, 1_000_000).unwrap();
    terminal.enable_kitty_graphics().unwrap();
    terminal.resize(10, 5, 8, 16).unwrap();

    assert_eq!(terminal.kitty_graphics_generation().unwrap(), 0);
    assert!(terminal.kitty_image_placements().unwrap().is_empty());

    terminal.write(b"\x1b_Ga=t,t=d,f=24,i=1,s=1,v=2;////////\x1b\\");
    let transmitted = terminal.kitty_graphics_generation().unwrap();
    assert_ne!(transmitted, 0);
    assert!(terminal.kitty_image_placements().unwrap().is_empty());
    assert_eq!(terminal.kitty_empty_generation.get(), Some(transmitted));

    terminal.write(b"plain text");
    assert_eq!(terminal.kitty_graphics_generation().unwrap(), transmitted);
    assert!(terminal.kitty_image_placements().unwrap().is_empty());

    terminal.write(b"\x1b_Ga=p,i=1,p=1,c=1,r=1;\x1b\\");
    let placed = terminal.kitty_graphics_generation().unwrap();
    assert_ne!(placed, transmitted);
    assert_eq!(terminal.kitty_image_placements().unwrap().len(), 1);

    terminal.resize(10, 5, 12, 24).unwrap();
    assert_eq!(terminal.kitty_graphics_generation().unwrap(), placed);
    assert_eq!(terminal.kitty_image_placements().unwrap().len(), 1);

    write_numbered_lines(&mut terminal, 20);
    assert_eq!(terminal.kitty_graphics_generation().unwrap(), placed);
    assert!(terminal.kitty_image_placements().unwrap().is_empty());
    assert_ne!(terminal.kitty_empty_generation.get(), Some(placed));
    terminal.scroll_viewport_row(0);
    assert_eq!(terminal.kitty_image_placements().unwrap().len(), 1);

    terminal.write(b"\x1b_Ga=d,d=A\x1b\\");
    let deleted = terminal.kitty_graphics_generation().unwrap();
    assert_ne!(deleted, placed);
    assert!(terminal.kitty_image_placements().unwrap().is_empty());
    assert_eq!(terminal.kitty_empty_generation.get(), Some(deleted));
}

#[test]
fn build_info_contract_matches_expected_vendored_features() {
    let _simd = build_info_bool(ffi::GhosttyBuildInfo_GHOSTTY_BUILD_INFO_SIMD);
    let _tmux_control_mode =
        build_info_bool(ffi::GhosttyBuildInfo_GHOSTTY_BUILD_INFO_TMUX_CONTROL_MODE);
    let _kitty_graphics = build_info_bool(ffi::GhosttyBuildInfo_GHOSTTY_BUILD_INFO_KITTY_GRAPHICS);

    let optimize = build_info_optimize();
    assert!(matches!(
        optimize,
        ffi::GhosttyOptimizeMode_GHOSTTY_OPTIMIZE_DEBUG
            | ffi::GhosttyOptimizeMode_GHOSTTY_OPTIMIZE_RELEASE_SAFE
            | ffi::GhosttyOptimizeMode_GHOSTTY_OPTIMIZE_RELEASE_SMALL
            | ffi::GhosttyOptimizeMode_GHOSTTY_OPTIMIZE_RELEASE_FAST
    ));
}

#[test]
fn kitty_graphics_direct_rgba_placement_is_queryable() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.enable_kitty_graphics().unwrap();
    terminal.resize(10, 5, 8, 16).unwrap();
    terminal.write(b"\x1b_Ga=T,f=32,t=d,i=7,p=3,s=1,v=1,c=10,r=5,q=2;/wAA/w==\x1b\\");

    let placements = terminal.kitty_image_placements().unwrap();
    assert_eq!(placements.len(), 1);
    assert_eq!(placements[0].image_id, 7);
    assert_eq!(placements[0].placement_id, 3);
    assert_eq!(placements[0].image_width, 1);
    assert_eq!(placements[0].image_height, 1);
    assert_eq!(placements[0].format, KittyImageFormat::Rgba);
    assert_eq!(placements[0].data, [255, 0, 0, 255]);
    assert_eq!(placements[0].render.grid_cols, 10);
    assert_eq!(placements[0].render.grid_rows, 5);
}

#[test]
fn kitty_graphics_local_media_are_enabled() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.enable_kitty_graphics().unwrap();

    assert!(terminal
        .get_bool(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_FILE)
        .unwrap());
    assert!(terminal
        .get_bool(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_TEMP_FILE)
        .unwrap());
    assert!(terminal
        .get_bool(ffi::GhosttyTerminalData_GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_SHARED_MEM)
        .unwrap());
}

#[test]
fn kitty_graphics_file_medium_rgba_placement_is_queryable() {
    use base64::Engine;

    let dir = std::env::temp_dir().join(format!(
        "gterm-kitty-file-medium-test-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("pixel.rgba");
    std::fs::write(&path, [255, 0, 0, 255]).unwrap();

    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.enable_kitty_graphics().unwrap();
    terminal.resize(10, 5, 8, 16).unwrap();
    let encoded_path =
        base64::engine::general_purpose::STANDARD.encode(path.as_os_str().as_encoded_bytes());
    let command = format!("\x1b_Ga=T,f=32,t=f,i=9,p=4,s=1,v=1,c=10,r=5,q=2;{encoded_path}\x1b\\");
    terminal.write(command.as_bytes());
    terminal.write(b"\x1b_Ga=p,U=1,i=9,c=10,r=5\x1b\\");

    let placements = terminal.kitty_image_placements().unwrap();
    assert_eq!(placements.len(), 1);
    assert_eq!(placements[0].image_id, 9);
    assert_eq!(placements[0].placement_id, 4);
    assert_eq!(placements[0].image_width, 1);
    assert_eq!(placements[0].image_height, 1);
    assert_eq!(placements[0].format, KittyImageFormat::Rgba);
    assert_eq!(placements[0].data, [255, 0, 0, 255]);
    assert_eq!(placements[0].render.grid_cols, 10);
    assert_eq!(placements[0].render.grid_rows, 5);

    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn kitty_graphics_unicode_placeholder_placement_is_queryable() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.enable_kitty_graphics().unwrap();
    terminal.resize(10, 5, 8, 16).unwrap();
    terminal.write(b"\x1b_Gq=2,a=t,t=d,f=32,s=1,v=1,i=1193046,m=0;/wAA/w==\x1b\\");
    terminal.write(b"\x1b_Gq=2,a=p,U=1,i=1193046,c=2,r=1\x1b\\");
    terminal.write(
        "\x1b[2;3H\x1b[38;2;18;52;86m\u{10eeee}\u{0305}\u{0305}\u{10eeee}\u{0305}\u{030d}\x1b[0m"
            .as_bytes(),
    );

    let placements = terminal.kitty_image_placements().unwrap();
    assert_eq!(placements.len(), 1);
    assert_eq!(placements[0].image_id, 1193046);
    assert_ne!(placements[0].placement_id, 0);
    assert_eq!(placements[0].image_width, 1);
    assert_eq!(placements[0].image_height, 1);
    assert_eq!(placements[0].format, KittyImageFormat::Rgba);
    assert_eq!(placements[0].data, [255, 0, 0, 255]);
    assert_eq!(placements[0].render.viewport_col, 2);
    assert_eq!(placements[0].render.viewport_row, 1);
    assert_eq!(placements[0].render.grid_cols, 2);
    assert_eq!(placements[0].render.grid_rows, 1);
}

#[test]
fn unicode_width_helpers_match_terminal_layout_rules() {
    assert_eq!(unicode_codepoint_width('A' as u32), 1);
    assert_eq!(unicode_codepoint_width('\u{301}' as u32), 0);
    assert_eq!(unicode_codepoint_width('界' as u32), 2);
    assert_eq!(unicode_codepoint_width(0x11_0000), 1);

    let cases: &[(&[u32], usize, u8)] = &[
        (&[], 0, 0),
        (&['e' as u32, '\u{301}' as u32], 2, 1),
        (&['⚠' as u32, '\u{fe0f}' as u32], 2, 2),
        (&['⚠' as u32, '\u{fe0e}' as u32], 2, 1),
        (&['🇧' as u32, '🇷' as u32], 2, 2),
        (&['👍' as u32, '🏽' as u32], 2, 2),
        (
            &[
                '👨' as u32,
                '\u{200d}' as u32,
                '👩' as u32,
                '\u{200d}' as u32,
                '👧' as u32,
            ],
            5,
            2,
        ),
        (&[0x11_0000, 'A' as u32], 1, 1),
    ];
    for &(codepoints, consumed, width) in cases {
        assert_eq!(unicode_grapheme_width(codepoints), (consumed, width));
    }
}

#[test]
fn focus_encoding_matches_expected_sequences() {
    assert_eq!(encode_focus(FocusEvent::Gained).unwrap(), b"\x1b[I");
    assert_eq!(encode_focus(FocusEvent::Lost).unwrap(), b"\x1b[O");
}

#[test]
fn terminal_callbacks_report_pty_responses_and_pwd_changes() {
    let mut terminal = Terminal::new(8, 3, 100).unwrap();
    let responses = std::sync::Arc::new(std::sync::Mutex::new(Vec::<u8>::new()));
    let sink = responses.clone();
    terminal
        .set_write_pty_callback(move |bytes| sink.lock().unwrap().extend_from_slice(bytes))
        .unwrap();

    terminal.write(b"\x1b[6n\x1b]7;file:///tmp/gterm\x07");

    let output = responses.lock().unwrap().clone();
    assert!(!output.is_empty());
    assert!(String::from_utf8_lossy(&output).contains("R"));
    assert_eq!(terminal.take_pwd_changes(), [b"file:///tmp/gterm".to_vec()]);
}

#[test]
fn key_and_mouse_encoders_follow_terminal_state() {
    let mut terminal = Terminal::new(80, 24, 0).unwrap();
    terminal.mode_set(1, true).unwrap();
    terminal.write(b"\x1b[>1u\x1b[?1000h\x1b[?1006h");

    assert!(terminal.mode_get(1).unwrap());
    assert_eq!(terminal.kitty_keyboard_flags().unwrap(), 1);
    assert!(terminal.mouse_tracking_enabled().unwrap());

    let mut key_encoder = KeyEncoder::new().unwrap();
    key_encoder.set_from_terminal(&terminal);
    let mut key_event = KeyEvent::new().unwrap();
    key_event.set_action(ffi::GhosttyKeyAction_GHOSTTY_KEY_ACTION_PRESS);
    key_event.set_key(KEY_A);
    key_event.set_mods(MOD_CTRL | MOD_SHIFT);
    key_event.set_utf8("A");
    key_event.set_unshifted_codepoint('a' as u32);
    let encoded_key = key_encoder.encode(&key_event).unwrap();
    assert_eq!(encoded_key, b"\x1b[97;6u");

    let mut mouse_encoder = MouseEncoder::new().unwrap();
    mouse_encoder.set_from_terminal(&terminal);
    mouse_encoder.set_size(80, 24, 1, 1);
    let mut mouse_event = MouseEvent::new().unwrap();
    mouse_event.set_action(ffi::GhosttyMouseAction_GHOSTTY_MOUSE_ACTION_PRESS);
    mouse_event.set_button(ffi::GhosttyMouseButton_GHOSTTY_MOUSE_BUTTON_LEFT);
    mouse_event.set_position(0.0, 0.0);
    let encoded_mouse = mouse_encoder.encode(&mouse_event).unwrap();
    assert_eq!(encoded_mouse, b"\x1b[<0;1;1M");
}

#[test]
fn terminal_read_text_viewport_unwraps_soft_wrapped_selection() {
    let mut terminal = Terminal::new(5, 3, 0).unwrap();
    terminal.write("1ABCD2EFGH3IJKL".as_bytes());

    let text = terminal.read_text_viewport((0, 1), (2, 2), false).unwrap();
    assert_eq!(text, "2EFGH3IJ");
}

#[test]
fn terminal_extracts_viewport_hyperlink_uri() {
    let mut terminal = Terminal::new(20, 3, 0).unwrap();
    terminal.write(b"\x1b]8;;https://example.com\x1b\\Link\x1b]8;;\x1b\\");

    assert_eq!(
        terminal.viewport_hyperlink_uri(0, 0).unwrap().as_deref(),
        Some("https://example.com")
    );
    assert_eq!(terminal.viewport_hyperlink_uri(4, 0).unwrap(), None);
}

#[test]
fn terminal_read_text_viewport_handles_wide_chars() {
    let mut terminal = Terminal::new(5, 3, 0).unwrap();
    terminal.write("1A⚡".as_bytes());

    let full = terminal.read_text_viewport((0, 0), (3, 0), false).unwrap();
    assert_eq!(full, "1A⚡");

    let through_wide_head = terminal.read_text_viewport((0, 0), (2, 0), false).unwrap();
    assert_eq!(through_wide_head, "1A⚡");

    let wide_only = terminal.read_text_viewport((3, 0), (3, 0), false).unwrap();
    assert_eq!(wide_only, "⚡");
}

#[test]
fn zero_max_scrollback_disables_history() {
    let mut terminal = Terminal::new(80, 3, 0).unwrap();
    write_numbered_lines(&mut terminal, 3000);
    assert_eq!(terminal.scrollback_rows().unwrap(), 0);
}

#[test]
fn max_scrollback_limit_bytes_retains_more_history_for_larger_limits() {
    let mut small = Terminal::new(80, 3, 1_000_000).unwrap();
    let mut large = Terminal::new(80, 3, 10_000_000).unwrap();

    write_padded_lines(&mut small, 1_250, 70);
    write_padded_lines(&mut large, 1_250, 70);

    let small_scrollback = small.scrollback_rows().unwrap();
    let large_scrollback = large.scrollback_rows().unwrap();

    assert!(
            large_scrollback > small_scrollback,
            "expected larger byte limit to retain more history, got small={small_scrollback}, large={large_scrollback}"
        );
}

#[test]
fn large_negative_scroll_delta_reaches_top_of_scrollback() {
    let mut terminal = Terminal::new(80, 3, 1_000_000).unwrap();
    write_numbered_lines(&mut terminal, 1000);

    let before = terminal.scrollbar().unwrap();
    assert!(before.total > before.len);

    terminal.scroll_viewport_bottom();
    terminal.scroll_viewport_delta(-10_000);

    let after = terminal.scrollbar().unwrap();
    assert_eq!(after.offset, 0);
    assert_eq!(after.len, before.len);
}

#[test]
fn absolute_scroll_row_round_trips_and_clamps() {
    let mut terminal = Terminal::new(80, 3, 1_000_000).unwrap();
    write_numbered_lines(&mut terminal, 1000);

    let before = terminal.scrollbar().unwrap();
    let max_row = before.total.saturating_sub(before.len);
    assert!(max_row > 0);

    for row in [0, max_row / 2, max_row, usize::MAX] {
        terminal.scroll_viewport_row(row);
        let after = terminal.scrollbar().unwrap();
        assert_eq!(after.offset, row.min(max_row));
        assert_eq!(after.len, before.len);
    }
}

#[test]
fn deep_scrollback_resize_preserves_unicode_and_hyperlinks() {
    use std::fmt::Write as _;

    let mut terminal = Terminal::new(20, 5, 100_000_000).unwrap();
    let mut input = String::from("\x1b]8;;https://example.com\x1b\\FIRST 🇧🇷\x1b]8;;\x1b\\\r\n");
    for line in 0..70_000 {
        writeln!(input, "{line:05} 👨‍👩‍👧").unwrap();
    }
    terminal.write(input.as_bytes());

    assert!(terminal.scrollback_rows().unwrap() > u16::MAX as usize);
    terminal.scroll_viewport_delta(-100_000);
    assert_eq!(terminal.scrollbar().unwrap().offset, 0);
    assert!(terminal
        .read_text_viewport((0, 0), (19, 0), false)
        .unwrap()
        .starts_with("FIRST 🇧🇷"));
    assert_eq!(
        terminal.viewport_hyperlink_uri(0, 0).unwrap().as_deref(),
        Some("https://example.com")
    );

    terminal.resize(10, 5, 8, 16).unwrap();
    terminal.scroll_viewport_delta(-100_000);
    let metrics = terminal.scrollbar().unwrap();
    assert_eq!(metrics.offset, 0);
    assert_eq!(metrics.len, 5);
    assert!(terminal
        .read_text_viewport((0, 0), (9, 0), false)
        .unwrap()
        .starts_with("FIRST 🇧🇷"));
    assert_eq!(
        terminal.viewport_hyperlink_uri(0, 0).unwrap().as_deref(),
        Some("https://example.com")
    );
}

#[test]
fn active_screen_and_cursor_visibility_contract() {
    let mut terminal = Terminal::new(12, 3, 0).unwrap();
    let mut render_state = RenderState::new().unwrap();

    terminal.write(b"primary");
    assert_eq!(terminal.active_screen().unwrap(), ActiveScreen::Primary);
    assert_eq!(
        terminal.read_text_viewport((0, 0), (6, 0), false).unwrap(),
        "primary"
    );

    render_state.update(&terminal).unwrap();
    assert!(render_state.cursor_visible().unwrap());
    terminal.write(b"\x1b[?25l");
    render_state.update(&terminal).unwrap();
    assert!(!render_state.cursor_visible().unwrap());

    terminal.write(b"\x1b[?1049h\x1b[HALT");
    assert_eq!(terminal.active_screen().unwrap(), ActiveScreen::Alternate);
    assert_eq!(
        terminal.read_text_viewport((0, 0), (2, 0), false).unwrap(),
        "ALT"
    );

    terminal.write(b"\x1b[?1049l");
    assert_eq!(terminal.active_screen().unwrap(), ActiveScreen::Primary);
    assert_eq!(
        terminal.read_text_viewport((0, 0), (6, 0), false).unwrap(),
        "primary"
    );
}

#[test]
fn terminal_and_render_state_smoke_test() {
    let mut terminal = Terminal::new(8, 3, 100).unwrap();
    assert_eq!(terminal.cols().unwrap(), 8);
    assert_eq!(terminal.rows().unwrap(), 3);

    terminal.write(b"hello\r\nworld");

    let mut render_state = RenderState::new().unwrap();
    render_state.update(&terminal).unwrap();
    assert_eq!(render_state.cols().unwrap(), 8);
    assert_eq!(render_state.rows().unwrap(), 3);
    assert_ne!(render_state.dirty().unwrap(), Dirty::Clean);

    let mut row_iterator = RowIterator::new().unwrap();
    let mut row_iter = render_state
        .populate_row_iterator(&mut row_iterator)
        .unwrap();
    let mut row_cells = RowCells::new().unwrap();

    let mut found_hello = false;
    let mut found_world = false;
    let mut row_index = 0usize;
    while row_iter.next() {
        let _ = row_iter.dirty().unwrap();
        let mut cells = row_iter.populate_cells(&mut row_cells).unwrap();
        let mut line = String::new();
        while cells.next() {
            let text = cells.grapheme_text().unwrap();
            if text.is_empty() {
                line.push(' ');
            } else {
                line.push_str(&text);
            }
        }
        let trimmed = line.trim_end().to_string();
        if row_index == 0 {
            found_hello = trimmed.starts_with("hello");
        }
        if row_index == 1 {
            found_world = trimmed.starts_with("world");
        }
        row_index += 1;
    }

    assert!(found_hello);
    assert!(found_world);

    render_state.set_dirty(Dirty::Clean).unwrap();
    assert_eq!(render_state.dirty().unwrap(), Dirty::Clean);
}

#[test]
fn render_cells_preserve_issue_453_unicode_payload_exactly() {
    const PAYLOAD: &str = "README 👨‍👩‍👧‍👦 🧑‍💻 ✅ ⚡ 漢字 café é 🏳️‍🌈 🚀";
    let mut terminal = Terminal::new(80, 3, 100).unwrap();
    assert!(terminal.mode_get(MODE_GRAPHEME_CLUSTER).unwrap());
    terminal.write(format!("{PAYLOAD}\r\n").as_bytes());

    assert_eq!(first_rendered_row_text(&terminal), PAYLOAD);
}

#[test]
fn grapheme_cluster_mode_is_default_and_survives_full_reset() {
    let mut terminal = Terminal::new(80, 3, 100).unwrap();
    assert!(terminal.mode_get(MODE_GRAPHEME_CLUSTER).unwrap());

    terminal.write(b"\x1bc");

    assert!(terminal.mode_get(MODE_GRAPHEME_CLUSTER).unwrap());
}

#[test]
fn screen_text_rows_preserve_wrap_and_grapheme_cells() {
    let mut terminal = Terminal::new(5, 3, 100).unwrap();
    terminal.write("abcdef\r\n界e\u{301}".as_bytes());

    let rows = terminal.screen_text_rows().unwrap();

    assert_eq!(rows.len(), 3);
    assert!(rows[0].soft_wrapped);
    assert!(!rows[0].wrap_continuation);
    assert!(!rows[1].soft_wrapped);
    assert!(rows[1].wrap_continuation);
    assert!(!rows[2].wrap_continuation);
    assert_eq!(rows[2].cells[0].wide, CellWide::Wide);
    assert_eq!(rows[2].cells[0].graphemes, vec!['界' as u32]);
    assert_eq!(rows[2].cells[1].wide, CellWide::SpacerTail);
    assert_eq!(rows[2].cells[2].graphemes, vec!['e' as u32, 0x301]);
}

#[test]
fn render_state_row_dirty_can_be_cleared_independently() {
    let mut terminal = Terminal::new(8, 3, 100).unwrap();
    let mut render_state = RenderState::new().unwrap();

    render_state.update(&terminal).unwrap();
    {
        let mut row_iterator = RowIterator::new().unwrap();
        let mut rows = render_state
            .populate_row_iterator(&mut row_iterator)
            .unwrap();
        while rows.next() {
            rows.clear_dirty().unwrap();
            assert!(!rows.dirty().unwrap());
        }
    }
    render_state.set_dirty(Dirty::Clean).unwrap();
    assert_eq!(render_state.dirty().unwrap(), Dirty::Clean);

    terminal.write(b"A");
    render_state.update(&terminal).unwrap();
    assert_eq!(render_state.dirty().unwrap(), Dirty::Partial);

    let mut dirty_rows = 0usize;
    {
        let mut row_iterator = RowIterator::new().unwrap();
        let mut rows = render_state
            .populate_row_iterator(&mut row_iterator)
            .unwrap();
        while rows.next() {
            if rows.dirty().unwrap() {
                dirty_rows += 1;
                rows.clear_dirty().unwrap();
                assert!(!rows.dirty().unwrap());
            }
        }
    }
    assert_eq!(dirty_rows, 1);
    assert_eq!(render_state.dirty().unwrap(), Dirty::Partial);

    render_state.set_dirty(Dirty::Clean).unwrap();
    assert_eq!(render_state.dirty().unwrap(), Dirty::Clean);
}

#[test]
fn row_selection_returns_none_without_selection() {
    let terminal = Terminal::new(8, 3, 100).unwrap();
    let mut render_state = RenderState::new().unwrap();
    render_state.update(&terminal).unwrap();

    let mut row_iterator = RowIterator::new().unwrap();
    let mut rows = render_state
        .populate_row_iterator(&mut row_iterator)
        .unwrap();
    assert!(rows.next());
    assert_eq!(rows.selection().unwrap(), None);
}

fn test_clipboard_content(mime: &[u8], data: &[u8]) -> ffi::GhosttyClipboardContent {
    ffi::GhosttyClipboardContent {
        mime: ffi::GhosttyString {
            ptr: mime.as_ptr(),
            len: mime.len(),
        },
        data: ffi::GhosttyString {
            ptr: data.as_ptr(),
            len: data.len(),
        },
    }
}

fn invoke_clipboard_callback(
    terminal: &mut Terminal,
    contents: &[ffi::GhosttyClipboardContent],
    size: usize,
) -> ffi::GhosttyClipboardWriteResult {
    let request = ffi::GhosttyClipboardWrite {
        size,
        location: ffi::GhosttyClipboardLocation_GHOSTTY_CLIPBOARD_LOCATION_STANDARD,
        contents: contents.as_ptr(),
        contents_len: contents.len(),
    };
    // SAFETY: the request and its borrowed content live through this call.
    unsafe {
        clipboard_write_trampoline(
            terminal.raw,
            (&mut *terminal.callback_state as *mut TerminalCallbackState).cast(),
            &request,
        )
    }
}

#[test]
fn clipboard_callback_ignores_clear_and_rejects_unsupported_writes() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    let full_size = std::mem::size_of::<ffi::GhosttyClipboardWrite>();
    let success = ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS;
    let unsupported = ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    let invalid = ffi::GhosttyClipboardWriteResult_GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;

    assert_eq!(
        invoke_clipboard_callback(&mut terminal, &[], full_size),
        success
    );
    assert!(terminal.take_clipboard_writes().is_empty());

    let empty = test_clipboard_content(b"text/plain", b"");
    assert_eq!(
        invoke_clipboard_callback(&mut terminal, &[empty], full_size),
        unsupported
    );
    let text = test_clipboard_content(b"text/plain", b"text");
    let image = test_clipboard_content(b"image/png", b"image");
    assert_eq!(
        invoke_clipboard_callback(&mut terminal, &[text, image], full_size),
        unsupported
    );

    let oversized = vec![b'x'; MAX_CLIPBOARD_BYTES + 1];
    let oversized = test_clipboard_content(b"text/plain", &oversized);
    assert_eq!(
        invoke_clipboard_callback(&mut terminal, &[oversized], full_size),
        invalid
    );
    assert_eq!(
        invoke_clipboard_callback(&mut terminal, &[text], full_size - 1),
        invalid
    );
    assert!(terminal.take_clipboard_writes().is_empty());
}

#[test]
fn libghostty_completes_osc52_writes_for_bel_and_st_without_queries() {
    let mut terminal = Terminal::new(10, 5, 0).unwrap();
    terminal.write(b"\x1b]52;c;aGVs");
    assert!(terminal.take_clipboard_writes().is_empty());
    terminal.write(b"bG8=\x07");
    assert_eq!(terminal.take_clipboard_writes(), vec![b"hello".to_vec()]);

    terminal.write(b"\x1b]52;c;d29ybGQ=\x1b\\");
    assert_eq!(terminal.take_clipboard_writes(), vec![b"world".to_vec()]);

    terminal.write(b"\x1b]52;c;?\x07");
    assert!(terminal.take_clipboard_writes().is_empty());

    terminal.write(b"\x1b]52;c;\x07");
    assert!(terminal.take_clipboard_writes().is_empty());
}

#[test]
fn row_cell_basic_data_uses_batched_vendor_reads() {
    let mut terminal = Terminal::new(8, 3, 100).unwrap();
    terminal.write(b"\x1b[31mA\x1b[0m");

    let mut render_state = RenderState::new().unwrap();
    render_state.update(&terminal).unwrap();

    let mut row_iterator = RowIterator::new().unwrap();
    let mut rows = render_state
        .populate_row_iterator(&mut row_iterator)
        .unwrap();
    assert!(rows.next());

    let mut row_cells = RowCells::new().unwrap();
    let mut cells = rows.populate_cells(&mut row_cells).unwrap();
    assert!(cells.next());

    let basic = cells.basic_data().unwrap();
    assert_eq!(basic.wide, CellWide::Narrow);
    assert!(basic.has_styling);
    assert_eq!(basic.style.fg_color, Some(CellColor::Palette(1)));
    assert!(!basic.has_hyperlink);
}
