use super::*;
use crate::protocol::{CellData, CursorState};

const WIDE_GRAPHEME: &str = "💡";
const HALFWIDTH_VOICED_KANA: &str = "ｶ\u{ff9e}";

fn make_cell(symbol: &str, fg: u32, bg: u32, modifier: u16) -> CellData {
    CellData {
        symbol: symbol.to_owned(),
        fg,
        bg,
        modifier,
        skip: false,
        hyperlink: None,
    }
}

fn make_skip_cell(symbol: &str, fg: u32, bg: u32, modifier: u16) -> CellData {
    let mut cell = make_cell(symbol, fg, bg, modifier);
    cell.skip = true;
    cell
}

fn make_frame(width: u16, height: u16, cells: Vec<CellData>) -> FrameData {
    FrameData {
        cells,
        width,
        height,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    }
}

fn linked_cell(symbol: &str, index: u32) -> CellData {
    let mut cell = make_cell(symbol, 0, 0, 0);
    cell.hyperlink = Some(index);
    cell
}

#[test]
fn color_to_sgr_fg_named_colors() {
    assert_eq!(color_to_sgr_fg(0x00_00_00_00), "39"); // Reset
    assert_eq!(color_to_sgr_fg(0x00_00_00_01), "30"); // Black
    assert_eq!(color_to_sgr_fg(0x00_00_00_02), "31"); // Red
    assert_eq!(color_to_sgr_fg(0x00_00_00_10), "97"); // White
}

#[test]
fn color_to_sgr_fg_indexed() {
    assert_eq!(color_to_sgr_fg(0x01_00_00_AB), "38;5;171");
}

#[test]
fn color_to_sgr_fg_rgb() {
    assert_eq!(color_to_sgr_fg(0x02_FF_80_40), "38;2;255;128;64");
}

#[test]
fn color_to_sgr_bg_named_colors() {
    assert_eq!(color_to_sgr_bg(0x00_00_00_00), "49"); // Reset
    assert_eq!(color_to_sgr_bg(0x00_00_00_01), "40"); // Black
    assert_eq!(color_to_sgr_bg(0x00_00_00_10), "107"); // White
}

#[test]
fn color_to_sgr_bg_rgb() {
    assert_eq!(color_to_sgr_bg(0x02_FF_80_40), "48;2;255;128;64");
}

#[test]
fn modifier_to_sgr_parts_bold() {
    let parts = modifier_to_sgr_parts(1); // BOLD
    assert!(parts.contains(&"1"));
}

#[test]
fn modifier_to_sgr_parts_italic() {
    let parts = modifier_to_sgr_parts(4); // ITALIC
    assert!(parts.contains(&"3"));
}

#[test]
fn modifier_to_sgr_parts_empty() {
    let parts = modifier_to_sgr_parts(0);
    assert!(parts.is_empty());
}

#[test]
fn build_sgr_produces_valid_sequence() {
    let sgr = build_sgr(0x00_00_00_02, 0x00_00_00_01, 1); // fg=Red, bg=Black, bold
    assert!(sgr.starts_with("\x1b["));
    assert!(sgr.ends_with("m"));
    assert!(sgr.contains("0")); // reset existing style first
    assert!(sgr.contains("1")); // bold
    assert!(sgr.contains("31")); // fg red
    assert!(sgr.contains("40")); // bg black
}

#[test]
fn build_sgr_resets_previous_modifiers_when_cell_is_plain() {
    assert_eq!(build_sgr(0x00_00_00_00, 0x00_00_00_00, 0), "\x1b[0;39;49m");
}

#[test]
fn build_sgr_preserves_curly_underline_style() {
    let modifier = crate::protocol::modifier_to_u16(
        crate::protocol::modifier_with_underline_style(ratatui::style::Modifier::UNDERLINED, 3),
    );

    assert_eq!(
        build_sgr(0x00_00_00_00, 0x00_00_00_00, modifier),
        "\x1b[0;4:3;39;49m"
    );
}

#[test]
fn cells_equal_identical() {
    let a = make_cell("A", 2, 1, 0);
    let b = make_cell("A", 2, 1, 0);
    assert!(cells_equal(&a, &b));
}

#[test]
fn cells_equal_different_symbol() {
    let a = make_cell("A", 2, 1, 0);
    let b = make_cell("B", 2, 1, 0);
    assert!(!cells_equal(&a, &b));
}

#[test]
fn cells_equal_different_color() {
    let a = make_cell("A", 2, 1, 0);
    let b = make_cell("A", 3, 1, 0);
    assert!(!cells_equal(&a, &b));
}

#[test]
fn blit_frame_hides_cursor_before_full_redraw_writes() {
    let frame = make_frame(
        2,
        2,
        vec![
            make_cell("H", 0, 0, 0),
            make_cell("i", 0, 0, 0),
            make_cell("!", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
        ],
    );

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.starts_with("\x1b[?2026h\x1b[?25l"),
        "should hide cursor inside synchronized frame painting during full redraw"
    );
}

#[test]
fn blit_frame_hides_cursor_before_diff_writes() {
    let prev = make_frame(
        2,
        2,
        vec![
            make_cell("H", 0, 0, 0),
            make_cell("i", 0, 0, 0),
            make_cell("!", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
        ],
    );

    let curr = make_frame(
        2,
        2,
        vec![
            make_cell("X", 0, 0, 0), // Changed
            make_cell("i", 0, 0, 0), // Same
            make_cell("!", 0, 0, 0), // Same
            make_cell(" ", 0, 0, 0), // Same
        ],
    );

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.starts_with("\x1b[?2026h\x1b[?25l"),
        "should hide cursor inside synchronized frame painting during diff"
    );
}

#[test]
fn blit_frame_wraps_frame_in_synchronized_output() {
    let frame = make_frame(1, 1, vec![make_cell("A", 0, 0, 0)]);

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.starts_with("\x1b[?2026h\x1b[?25l"),
        "should begin synchronized output before frame writes"
    );
    let sync_end = output_str
        .find("\x1b[?2026l")
        .expect("should end synchronized output after frame writes");
    assert!(
        sync_end > 0,
        "should end synchronized output after frame writes"
    );
}

#[test]
fn blit_frame_begins_sync_before_hiding_cursor_after_visible_cursor_repeat() {
    let visible = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 2,
            y: 1,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let mut changed = visible.clone();
    changed.cells[0] = make_cell("B", 0, 0, 0);

    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut first_output = Vec::new();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut first_output,
        &visible,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );

    let mut second_output = Vec::new();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut second_output,
        &changed,
        Some(&visible),
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );

    let second_output_str = std::str::from_utf8(&second_output).unwrap();
    assert!(
        second_output_str.starts_with("\x1b[?2026h\x1b[?25l"),
        "next frame should enter synchronized output before hiding the cursor"
    );

    let hide = second_output_str
        .find("\x1b[?25l")
        .expect("second frame should hide cursor before painting");
    let first_paint = second_output_str
        .find("\x1b[1;1H")
        .expect("second frame should paint changed cell");
    assert!(
        hide < first_paint,
        "cursor should still hide before painting"
    );

    first_output.extend_from_slice(&second_output);
    let combined = String::from_utf8(first_output).unwrap();
    assert!(
        combined.contains("\x1b[?2026l\x1b[2;3H\x1b[?25h\x1b[?2026h\x1b[?25l"),
        "post-sync cursor repeat should be followed by a synchronized cursor hide"
    );
}

#[test]
fn blit_frame_can_repeat_final_cursor_state_after_synchronized_output() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 2,
            y: 1,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut output,
        &frame,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    let sync_end = output_str
        .find("\x1b[?2026l")
        .expect("should end synchronized output");
    let trailing_cursor = &output_str[sync_end + "\x1b[?2026l".len()..];
    assert_eq!(
        trailing_cursor, "\x1b[2;3H\x1b[?25h",
        "should expose only the final cursor state after synchronized output"
    );
}

#[test]
fn blit_frame_can_skip_final_cursor_state_after_synchronized_output() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 2,
            y: 1,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut output,
        &frame,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        false,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    let sync_end = output_str
        .find("\x1b[?2026l")
        .expect("should end synchronized output");
    let trailing_cursor = &output_str[sync_end + "\x1b[?2026l".len()..];
    assert_eq!(
        trailing_cursor, "",
        "should not expose a post-sync cursor repeat when the target terminal flickers on it"
    );
}

#[test]
fn drawn_cursor_reverses_visible_cursor_cell() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 2,
            y: 1,
            visible: true,
            shape: 6,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let drawn = frame_with_drawn_cursor(frame.clone());

    assert_eq!(drawn.cells[5].modifier, REVERSED_MODIFIER);
    assert_eq!(frame.cells[5].modifier, 0);

    let encoded = BlitEncoder::new().encode_with_suppressed_visible_cursor(&drawn, false);
    let output_str = String::from_utf8(encoded.bytes).unwrap();

    assert!(
        output_str.contains("\x1b[2;3H\x1b[6 q\x1b[?25l"),
        "drawn cursor mode should park the host cursor hidden at the focused cursor position"
    );
    assert!(
        !output_str.contains("\x1b[?25h"),
        "drawn cursor mode should not show the host cursor"
    );
    assert!(
        output_str.contains("\x1b[0;7;39;49mA"),
        "drawn cursor should be emitted as reverse-video cell content"
    );
}

#[test]
fn drawn_cursor_ignores_hidden_cursor() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: false,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    assert_eq!(frame_with_drawn_cursor(frame.clone()), frame);
}

#[test]
fn blit_frame_emits_cursor_shape_before_visibility_without_touching_ime_anchor() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 6,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut output,
        &frame,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    let final_cursor = output_str
        .find("\x1b[1;1H\x1b[6 q\x1b[?25h")
        .expect("should set cursor shape before showing cursor");
    let sync_end = output_str
        .find("\x1b[?2026l")
        .expect("should end synchronized output");
    assert!(
        final_cursor < sync_end,
        "shape should be part of the synchronized final cursor state"
    );
    let trailing_cursor = &output_str[sync_end + "\x1b[?2026l".len()..];
    assert_eq!(
        trailing_cursor, "\x1b[1;1H\x1b[?25h",
        "IME anchor update should preserve the existing position/visibility-only contract"
    );
}

#[test]
fn blit_frame_repeats_explicit_hidden_cursor_anchor_after_synchronized_output() {
    let visible = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let hidden = FrameData {
        cells: vec![make_cell("B", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 2,
            y: 1,
            visible: false,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();

    blit_frame_to_with_cursor_memory_and_policy(
        &mut output,
        &visible,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );
    output.clear();
    blit_frame_to_with_cursor_memory_and_policy(
        &mut output,
        &hidden,
        Some(&visible),
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        true,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    let sync_end = output_str
        .find("\x1b[?2026l")
        .expect("should end synchronized output");
    let trailing_cursor = &output_str[sync_end + "\x1b[?2026l".len()..];
    assert_eq!(
        trailing_cursor, "\x1b[2;3H\x1b[?25l",
        "should repeat the explicit hidden cursor position while preserving visibility"
    );
}

#[test]
fn blit_frame_emits_osc8_for_linked_cells() {
    let mut frame = make_frame(
        3,
        1,
        vec![
            linked_cell("L", 0),
            linked_cell("i", 0),
            make_cell("!", 0, 0, 0),
        ],
    );
    frame.hyperlinks.push("https://example.com".to_owned());

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(output_str.contains("\x1b]8;;https://example.com\x1b\\L"));
    assert!(output_str.contains('i'));
    assert!(output_str.contains("\x1b]8;;\x1b\\"));
}

#[test]
fn blit_frame_sanitizes_hyperlink_uris() {
    let mut frame = make_frame(1, 1, vec![linked_cell("L", 0)]);
    frame
        .hyperlinks
        .push("https://exa\x1b\x07mple.com".to_owned());

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(output_str.contains("\x1b]8;;https://example.com\x1b\\L"));
}

#[test]
fn blit_frame_first_frame_produces_output() {
    let frame = make_frame(
        2,
        2,
        vec![
            make_cell("H", 0, 0, 0),
            make_cell("i", 0, 0, 0),
            make_cell("!", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
        ],
    );

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    // Full redraw should start with clear screen.
    assert!(
        output_str.contains("\x1b[2J"),
        "full redraw should clear screen"
    );
    assert!(
        output_str.contains('H') || output_str.contains('i'),
        "should contain cell content"
    );
}

#[test]
fn blit_frame_diff_only_writes_changed_cells() {
    let prev = make_frame(
        2,
        2,
        vec![
            make_cell("H", 0, 0, 0),
            make_cell("i", 0, 0, 0),
            make_cell("!", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
        ],
    );

    // Only the first cell changed.
    let curr = make_frame(
        2,
        2,
        vec![
            make_cell("X", 0, 0, 0), // Changed
            make_cell("i", 0, 0, 0), // Same
            make_cell("!", 0, 0, 0), // Same
            make_cell(" ", 0, 0, 0), // Same
        ],
    );

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));

    let output_str = String::from_utf8(output).unwrap();
    // Diff should NOT clear the screen.
    assert!(
        !output_str.contains("\x1b[2J"),
        "diff should not clear screen"
    );
    // Should contain the changed cell content.
    assert!(output_str.contains('X'), "should contain changed cell 'X'");
}

#[test]
fn scroll_sized_ascii_shift_batches_changed_cells_by_row() {
    const WIDTH: u16 = 140;
    const HEIGHT: u16 = 50;
    let prev = make_frame(
        WIDTH,
        HEIGHT,
        vec![make_cell("A", 0, 0, 0); usize::from(WIDTH) * usize::from(HEIGHT)],
    );
    let curr = make_frame(
        WIDTH,
        HEIGHT,
        vec![make_cell("B", 0, 0, 0); usize::from(WIDTH) * usize::from(HEIGHT)],
    );

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));

    let cup_count = output.iter().filter(|&&byte| byte == b'H').count();
    assert!(
            cup_count <= usize::from(HEIGHT) + 2,
            "one dense scroll frame should need at most one CUP per row plus cursor anchors, got {cup_count}"
        );
    assert!(
            output.len() <= 16_290,
            "one dense scroll frame should stay below 25% of the 65,161-byte live baseline, got {} bytes",
            output.len()
        );
}

#[cfg(feature = "vt-engine")]
#[test]
fn batched_ascii_diff_replays_to_current_frame() {
    let prev = make_frame(4, 3, vec![make_cell("A", 0, 0, 0); 12]);
    let curr = make_frame(4, 3, vec![make_cell("B", 0, 0, 0); 12]);
    let mut terminal = crate::ghostty::Terminal::new(4, 3, 0).unwrap();

    let mut initial = Vec::new();
    blit_frame_to(&mut initial, &prev, None);
    terminal.write(&initial);

    let mut diff = Vec::new();
    blit_frame_to(&mut diff, &curr, Some(&prev));
    terminal.write(&diff);

    for row in 0..3 {
        for col in 0..4 {
            let (_, graphemes) = terminal.screen_cell(col, row).unwrap();
            assert_eq!(graphemes, vec![u32::from('B')]);
        }
    }
}

#[test]
fn encoder_size_change_repaints_without_clearing() {
    let prev = make_frame(2, 2, vec![make_cell("A", 0, 0, 0); 4]);
    let curr = make_frame(3, 2, vec![make_cell("B", 0, 0, 0); 6]);
    let mut encoder = BlitEncoder::new();
    let initial = encoder.encode(&prev, false);
    encoder.commit(prev, initial);

    let encoded = encoder.encode(&curr, false);
    assert!(encoded.full);
    let output = String::from_utf8(encoded.bytes).unwrap();

    assert!(!output.contains("\x1b[2J"));
    assert!(output.bytes().filter(|byte| *byte == b'B').count() >= 6);
}

#[test]
fn encoder_forced_repaint_writes_all_cells_without_clearing() {
    let frame = make_frame(3, 2, vec![make_cell("A", 0, 0, 0); 6]);
    let mut encoder = BlitEncoder::new();
    let initial = encoder.encode(&frame, false);
    encoder.commit(frame.clone(), initial);

    let encoded = encoder.encode(&frame, true);
    assert!(encoded.full);
    let output = String::from_utf8(encoded.bytes).unwrap();

    assert!(!output.contains("\x1b[2J"));
    assert!(output.bytes().filter(|byte| *byte == b'A').count() >= 6);
}

#[test]
fn blit_frame_positions_cursor() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.contains("\x1b[1;1H"),
        "should position cursor at (1,1)"
    );
}

#[test]
fn blit_frame_hides_cursor_when_invisible() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: false,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.contains("\x1b[?25l"),
        "should hide cursor when invisible"
    );
}

#[test]
fn blit_frame_no_cursor_hides_cursor() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.contains("\x1b[?25l"),
        "should hide cursor when no cursor state"
    );
}

#[test]
fn blit_frame_restores_cursor_visibility() {
    // First frame: cursor hidden.
    let prev = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: false,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &prev, None);
    assert!(
        String::from_utf8(output).unwrap().contains("\x1b[?25l"),
        "first frame should hide cursor"
    );

    // Second frame: cursor visible — should restore visibility.
    let curr = FrameData {
        cells: vec![make_cell("B", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));
    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.contains("\x1b[?25h"),
        "second frame should restore cursor visibility with ?25h"
    );
    assert!(
        output_str.contains("\x1b[1;1H"),
        "should position cursor before showing it"
    );
}

#[test]
fn blit_frame_positions_cursor_before_showing_it() {
    let prev = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let mut curr = prev.clone();
    curr.cells[0] = make_cell("B", 0, 0, 0);
    curr.cursor = Some(CursorState {
        x: 2,
        y: 2,
        visible: true,
        shape: 0,
    });

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));
    let output_str = String::from_utf8(output).unwrap();
    let final_move = output_str
        .rfind("\x1b[3;3H")
        .expect("should move cursor to final position");
    let show = output_str
        .rfind("\x1b[?25h")
        .expect("should show cursor after positioning it");

    assert!(
        final_move < show,
        "should move cursor to final position before showing it"
    );
}

#[test]
fn blit_frame_parks_hidden_cursor_at_last_visible_position() {
    let visible = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: Some(CursorState {
            x: 1,
            y: 1,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let hidden = FrameData {
        cells: vec![make_cell("B", 0, 0, 0); 9],
        width: 3,
        height: 3,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();

    blit_frame_to_with_cursor_memory(
        &mut output,
        &visible,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        false,
    );
    output.clear();
    blit_frame_to_with_cursor_memory(
        &mut output,
        &hidden,
        Some(&visible),
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    let park = output_str
        .rfind("\x1b[2;2H")
        .expect("should park hidden cursor at last visible position");
    let hide = output_str
        .rfind("\x1b[?25l")
        .expect("should keep hidden cursor hidden");
    assert!(park < hide, "should park cursor before hiding it");
}

#[test]
fn blit_frame_parks_hidden_cursor_at_bottom_right_without_history() {
    let frame = FrameData {
        cells: vec![make_cell("A", 0, 0, 0); 6],
        width: 3,
        height: 2,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let mut last_visible_cursor = None;
    let mut last_cursor_shape = 0;
    let mut output = Vec::new();

    blit_frame_to_with_cursor_memory(
        &mut output,
        &frame,
        None,
        &mut last_visible_cursor,
        &mut last_cursor_shape,
        false,
    );

    let output_str = String::from_utf8(output).unwrap();
    assert!(
        output_str.contains("\x1b[2;3H\x1b[?25l"),
        "should park hidden cursor at bottom-right before ending the frame"
    );
}

#[test]
fn blit_frame_hides_previous_visible_cursor_when_next_frame_has_none() {
    let prev = FrameData {
        cells: vec![make_cell("A", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: Some(CursorState {
            x: 0,
            y: 0,
            visible: true,
            shape: 0,
        }),
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let curr = FrameData {
        cells: vec![make_cell("B", 0, 0, 0)],
        width: 1,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));

    assert!(
        String::from_utf8(output).unwrap().contains("\x1b[?25l"),
        "diff redraw should hide a previously visible cursor when the next frame has none"
    );
}

#[test]
fn full_redraw_skips_trailing_cells_covered_by_wide_graphemes() {
    let frame = FrameData {
        cells: vec![
            make_cell(WIDE_GRAPHEME, 0, 0, 0),
            make_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);
    let output_str = String::from_utf8(output).unwrap();

    assert!(output_str.contains("\x1b[1;1H"));
    assert!(!output_str.contains("\x1b[1;2H"));
    assert!(output_str.contains("\x1b[1;3H"));
}

#[test]
fn full_redraw_skips_trailing_cells_covered_by_halfwidth_voiced_kana() {
    let frame = FrameData {
        cells: vec![
            make_cell(HALFWIDTH_VOICED_KANA, 0, 0, 0),
            make_skip_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &frame, None);
    let output_str = String::from_utf8(output).unwrap();

    assert!(output_str.contains("\x1b[1;1H"));
    assert!(!output_str.contains("\x1b[1;2H"));
    assert!(output_str.contains("\x1b[1;3H"));
}

#[test]
fn diff_redraw_reveals_cells_hidden_by_previous_wide_graphemes() {
    let prev = FrameData {
        cells: vec![
            make_cell(WIDE_GRAPHEME, 0, 0, 0),
            make_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let curr = FrameData {
        cells: vec![
            make_cell("A", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));
    let output_str = String::from_utf8(output).unwrap();

    assert!(output_str.contains("\x1b[1;1H"));
    assert!(
        output_str.contains("\x1b[1;2H"),
        "cells hidden by a previous wide grapheme must be redrawn when they become visible"
    );
}

#[test]
fn diff_redraw_skips_new_trailing_cells_covered_by_wide_graphemes() {
    let prev = FrameData {
        cells: vec![
            make_cell("A", 0, 0, 0),
            make_cell("B", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let curr = FrameData {
        cells: vec![
            make_cell(WIDE_GRAPHEME, 0, 0, 0),
            make_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));
    let output_str = String::from_utf8(output).unwrap();

    assert!(output_str.contains("\x1b[1;1H"));
    assert!(!output_str.contains("\x1b[1;2H"));
}

#[test]
fn diff_redraw_reveals_cells_hidden_by_previous_halfwidth_voiced_kana() {
    let prev = FrameData {
        cells: vec![
            make_cell(HALFWIDTH_VOICED_KANA, 0, 0, 0),
            make_skip_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };
    let curr = FrameData {
        cells: vec![
            make_cell("A", 0, 0, 0),
            make_cell(" ", 0, 0, 0),
            make_cell("Z", 0, 0, 0),
        ],
        width: 3,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    };

    let mut output = Vec::new();
    blit_frame_to(&mut output, &curr, Some(&prev));
    let output_str = String::from_utf8(output).unwrap();

    assert!(output_str.contains("\x1b[1;1H"));
    assert!(
        output_str.contains("\x1b[1;2H"),
        "cells hidden by a previous halfwidth voiced kana must be redrawn when visible"
    );
}
