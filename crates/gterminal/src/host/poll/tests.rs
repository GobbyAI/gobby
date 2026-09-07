//! Escape-sequence handling in `capture_to_frame`.

use super::*;

fn cursor() -> CursorState {
    CursorState {
        x: 0,
        y: 0,
        visible: false,
        shape: 0,
    }
}

/// Read one row back as the text a viewer would see.
fn row(frame: &FrameData, y: u16) -> String {
    let width = frame.width as usize;
    let start = y as usize * width;
    frame.cells[start..start + width]
        .iter()
        .map(|cell| cell.symbol.as_str())
        .collect::<String>()
        .trim_end()
        .to_string()
}

/// tmux's `capture-pane -e` writes hyperlinks as OSC 8 with an ST terminator.
/// The parser used to consume only the ESC, so `]8;;<url>` and the ST's
/// trailing backslash were painted into the grid over the link text.
#[test]
fn an_st_terminated_osc_hyperlink_leaves_only_its_link_text() {
    let text =
        "\u{1b}[38;5;6;49m\u{1b}]8;;https://example.com/a\u{1b}\\Graphify\u{1b}]8;;\u{1b}\\ ok";
    let frame = capture_to_frame(text, 40, 2, PaneModes::default(), cursor());

    assert_eq!(row(&frame, 0), "Graphify ok");
    let rendered = row(&frame, 0);
    assert!(!rendered.contains("]8;;"), "OSC payload reached the grid");
    assert!(!rendered.contains('\\'), "ST terminator reached the grid");
}

/// The surrounding SGR still applies to the link text: dropping the escape
/// must not also drop the color state it was nested in.
#[test]
fn sgr_state_survives_an_osc_sequence() {
    let text = "\u{1b}[38;5;6;49m\u{1b}]8;;https://example.com/a\u{1b}\\G";
    let frame = capture_to_frame(text, 8, 1, PaneModes::default(), cursor());

    assert_eq!(frame.cells[0].symbol, "G");
    assert_eq!(frame.cells[0].fg, 0x01_00_00_06);
}

/// BEL is the other legal OSC terminator.
#[test]
fn a_bel_terminated_osc_hyperlink_leaves_only_its_link_text() {
    let text = "\u{1b}]8;;https://example.com/b\u{7}Archify\u{1b}]8;;\u{7}!";
    let frame = capture_to_frame(text, 40, 1, PaneModes::default(), cursor());

    assert_eq!(row(&frame, 0), "Archify!");
}

/// A non-string escape is intermediates plus one final byte; a charset
/// selection such as `ESC ( B` must contribute no cells, not a stray `B`.
#[test]
fn a_non_string_escape_contributes_no_cells() {
    let frame = capture_to_frame("a\u{1b}(Bb", 8, 1, PaneModes::default(), cursor());
    assert_eq!(row(&frame, 0), "ab");
}

/// CSI handling is unchanged: parameters are consumed and SGR still applies.
#[test]
fn csi_sequences_still_apply_sgr_without_leaking_parameters() {
    let frame = capture_to_frame(
        "\u{1b}[1;31mred\u{1b}[0m.",
        16,
        1,
        PaneModes::default(),
        cursor(),
    );

    assert_eq!(row(&frame, 0), "red.");
    assert_eq!(frame.cells[0].fg, 2);
    assert_eq!(frame.cells[0].modifier, 1);
    assert_eq!(frame.cells[3].fg, 0);
}
