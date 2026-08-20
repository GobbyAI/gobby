use super::*;
use ratatui::{layout::Rect, style::Color};
use tokio::sync::mpsc;

#[test]
fn plain_page_keys_host_scroll_for_shell_like_decckm_with_bracketed_paste() {
    assert!(InputState {
        alternate_screen: false,
        application_cursor: true,
        bracketed_paste: true,
        focus_reporting: false,
        mouse_protocol_mode: crate::input::MouseProtocolMode::None,
        mouse_protocol_encoding: crate::input::MouseProtocolEncoding::Default,
        mouse_alternate_scroll: false,
        modify_other_keys: false,
        color_scheme_reporting: false,
    }
    .plain_page_keys_use_host_scrollback());
}

fn text_cell(text: &str) -> crate::ghostty::ScreenTextCell {
    crate::ghostty::ScreenTextCell {
        wide: crate::ghostty::CellWide::Narrow,
        graphemes: text.chars().map(u32::from).collect(),
    }
}

fn wide_text_cells(text: &str) -> [crate::ghostty::ScreenTextCell; 2] {
    [
        crate::ghostty::ScreenTextCell {
            wide: crate::ghostty::CellWide::Wide,
            graphemes: text.chars().map(u32::from).collect(),
        },
        crate::ghostty::ScreenTextCell {
            wide: crate::ghostty::CellWide::SpacerTail,
            graphemes: Vec::new(),
        },
    ]
}

fn text_row(
    cells: impl IntoIterator<Item = crate::ghostty::ScreenTextCell>,
    soft_wrapped: bool,
) -> crate::ghostty::ScreenTextRow {
    crate::ghostty::ScreenTextRow {
        cells: cells.into_iter().collect(),
        soft_wrapped,
        wrap_continuation: false,
    }
}

fn search_primary(
    buffer: &RetainedTextBuffer,
    query: &str,
    case_sensitive: bool,
) -> Vec<TerminalTextMatch> {
    buffer.search(query, case_sensitive, crate::ghostty::ActiveScreen::Primary)
}

fn write_numbered_lines(terminal: &mut crate::ghostty::Terminal, count: usize) {
    for i in 0..count {
        terminal.write(format!("{i:06}\r\n").as_bytes());
    }
}

fn write_wrapped_contract_lines(terminal: &mut crate::ghostty::Terminal, count: usize) {
    for i in 0..count {
        terminal.write(format!("WRAP-{i:03}-abcdefghijklmnopqrstuvwxyz\r\n").as_bytes());
    }
    terminal.write(b"END");
}

#[test]
fn retained_text_search_crosses_soft_wraps_but_not_hard_lines() {
    let buffer = RetainedTextBuffer::new(
        5,
        vec![
            text_row("abcde".chars().map(|ch| text_cell(&ch.to_string())), true),
            text_row("fgh  ".chars().map(|ch| text_cell(&ch.to_string())), false),
            text_row("abc  ".chars().map(|ch| text_cell(&ch.to_string())), false),
        ],
    );

    let matches = search_primary(&buffer, "def", true);
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0].start, TerminalTextPoint { row: 0, col: 3 });
    assert_eq!(matches[0].end, TerminalTextPoint { row: 1, col: 0 });
    assert!(search_primary(&buffer, "hab", true).is_empty());
}

#[test]
fn retained_text_search_maps_wide_and_combining_graphemes_to_cells() {
    let mut cells = vec![text_cell("A")];
    cells.extend(wide_text_cells("界"));
    cells.push(text_cell("e\u{301}"));
    cells.push(text_cell("Z"));
    let buffer = RetainedTextBuffer::new(5, vec![text_row(cells, false)]);

    let matches = search_primary(&buffer, "界e\u{301}", true);
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0].start, TerminalTextPoint { row: 0, col: 1 });
    assert_eq!(matches[0].end, TerminalTextPoint { row: 0, col: 3 });
    assert!(search_primary(&buffer, "\u{301}", true).is_empty());
}

#[test]
fn retained_text_search_skips_wide_spacer_heads_at_soft_wraps() {
    let mut first = "abcd"
        .chars()
        .map(|ch| text_cell(&ch.to_string()))
        .collect::<Vec<_>>();
    first.push(crate::ghostty::ScreenTextCell {
        wide: crate::ghostty::CellWide::SpacerHead,
        graphemes: Vec::new(),
    });
    let mut second = wide_text_cells("界").to_vec();
    second.extend("xyz".chars().map(|ch| text_cell(&ch.to_string())));
    let buffer = RetainedTextBuffer::new(5, vec![text_row(first, true), text_row(second, false)]);

    let matches = search_primary(&buffer, "d界", true);
    assert_eq!(matches.len(), 1);
    assert_eq!(matches[0].start, TerminalTextPoint { row: 0, col: 3 });
    assert_eq!(matches[0].end, TerminalTextPoint { row: 1, col: 1 });
}

#[test]
fn retained_text_word_motion_does_not_split_at_a_wide_spacer_head() {
    let mut first = "abcd"
        .chars()
        .map(|ch| text_cell(&ch.to_string()))
        .collect::<Vec<_>>();
    first.push(crate::ghostty::ScreenTextCell {
        wide: crate::ghostty::CellWide::SpacerHead,
        graphemes: Vec::new(),
    });
    let mut second = wide_text_cells("界").to_vec();
    second.extend("xyz".chars().map(|ch| text_cell(&ch.to_string())));
    let buffer = RetainedTextBuffer::new(5, vec![text_row(first, true), text_row(second, false)]);

    assert_eq!(
        buffer.word_motion(0, 0, TerminalWordMotion::NextStart),
        None
    );
    assert_eq!(
        buffer.word_motion(0, 0, TerminalWordMotion::NextEnd),
        Some(TerminalTextPoint { row: 1, col: 4 })
    );
}

#[test]
fn retained_text_search_is_literal_and_unicode_case_aware() {
    let buffer = RetainedTextBuffer::new(
        12,
        vec![text_row(
            "CAFÉ a.b    ".chars().map(|ch| text_cell(&ch.to_string())),
            false,
        )],
    );

    assert_eq!(search_primary(&buffer, "café", false).len(), 1);
    assert!(search_primary(&buffer, "café", true).is_empty());
    assert_eq!(search_primary(&buffer, "a.b", true).len(), 1);
    assert!(search_primary(&buffer, "a?b", true).is_empty());
}

#[test]
fn retained_text_word_motions_use_tmux_separators_across_rows() {
    let buffer = RetainedTextBuffer::new(
        6,
        vec![
            text_row("a_b.c ".chars().map(|ch| text_cell(&ch.to_string())), false),
            text_row("—d    ".chars().map(|ch| text_cell(&ch.to_string())), false),
        ],
    );

    assert_eq!(
        buffer.word_motion(0, 0, TerminalWordMotion::NextStart),
        Some(TerminalTextPoint { row: 0, col: 3 })
    );
    assert_eq!(
        buffer.word_motion(0, 3, TerminalWordMotion::NextStart),
        Some(TerminalTextPoint { row: 0, col: 4 })
    );
    assert_eq!(
        buffer.word_motion(0, 4, TerminalWordMotion::NextStart),
        Some(TerminalTextPoint { row: 1, col: 0 })
    );
    assert_eq!(
        buffer.word_motion(1, 1, TerminalWordMotion::PreviousStart),
        Some(TerminalTextPoint { row: 1, col: 0 })
    );
}

#[test]
fn live_terminal_match_validation_rejects_overwritten_text() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 100).unwrap();
    terminal.write(b"alpha needle");
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());

    let text_match = pane.search_text_matches("needle", true)[0];
    assert!(pane.text_match_is_current(text_match));
    pane.ghostty
        .core
        .lock()
        .unwrap()
        .terminal
        .write(b"\r\x1b[2Kalpha changed");
    assert!(!pane.text_match_is_current(text_match));
}

#[test]
fn live_terminal_match_validation_handles_soft_wrapped_matches() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(5, 3, 100).unwrap();
    terminal.write(b"abcdef");
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());

    let text_match = pane.search_text_matches("def", true)[0];

    assert_eq!(text_match.start, TerminalTextPoint { row: 0, col: 3 });
    assert_eq!(text_match.end, TerminalTextPoint { row: 1, col: 0 });
    assert!(pane.text_match_is_current(text_match));
}

#[test]
fn live_terminal_match_validation_rejects_an_active_screen_change() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 100).unwrap();
    terminal.write(b"alpha needle");
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());

    let text_match = pane.search_text_matches("needle", true)[0];
    pane.ghostty
        .core
        .lock()
        .unwrap()
        .terminal
        .write(b"\x1b[?1049hneedle");

    assert!(!pane.text_match_is_current(text_match));
}

#[test]
fn live_terminal_word_motion_expands_across_long_blank_history() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(10, 3, 200).unwrap();
    terminal.write(b"origin\r\n");
    for _ in 0..80 {
        terminal.write(b"\r\n");
    }
    let last_row = terminal.total_rows().unwrap().saturating_sub(1) as u32;
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());

    assert_eq!(
        pane.word_motion_target(last_row, 0, TerminalWordMotion::PreviousStart),
        Some(TerminalTextPoint { row: 0, col: 0 })
    );
}

#[test]
fn live_terminal_word_end_expands_through_a_long_soft_wrap() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(2, 3, 200).unwrap();
    let word = "a".repeat(132);
    terminal.write(word.as_bytes());
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());
    let text_match = pane.search_text_matches(&word, true)[0];

    assert_eq!(
        pane.word_motion_target(
            text_match.start.row,
            text_match.start.col,
            TerminalWordMotion::NextEnd,
        ),
        Some(text_match.end)
    );
}

#[test]
fn live_terminal_word_end_expands_through_a_long_wide_soft_wrap() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(2, 3, 200).unwrap();
    let word = "界".repeat(66);
    terminal.write(word.as_bytes());
    let pane = PaneTerminal::new(GhosttyPaneTerminal::new(terminal, tx).unwrap());
    let text_match = pane.search_text_matches(&word, true)[0];

    // The word end sits on the head cell of the final wide glyph, past the
    // initial read window, so the window has to expand to reach it.
    assert_eq!(
        pane.word_motion_target(
            text_match.start.row,
            text_match.start.col,
            TerminalWordMotion::NextEnd,
        ),
        Some(TerminalTextPoint {
            row: text_match.end.row,
            col: 0,
        })
    );
}

fn current_palette_color(pane: &GhosttyPaneTerminal, index: u8) -> crate::ghostty::RgbColor {
    let mut core = pane.core.lock().unwrap();
    let GhosttyPaneCore {
        terminal,
        render_state,
        ..
    } = &mut *core;
    render_state.update(terminal).unwrap();
    render_state.colors().unwrap().palette[usize::from(index)]
}

fn expected_osc_rgb_response(command: &str, color: crate::ghostty::RgbColor) -> Bytes {
    let r = u16::from(color.r) * 257;
    let g = u16::from(color.g) * 257;
    let b = u16::from(color.b) * 257;
    Bytes::from(format!("\x1b]{command};rgb:{r:04x}/{g:04x}/{b:04x}\x1b\\"))
}

#[test]
fn process_pty_bytes_reports_latest_libghostty_pwd_callback() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let partial = pane.process_pty_bytes(pane_id, 0, b"\x1b]7;file:///tmp/gterm%20", &tx);
    assert_eq!(partial.reported_cwd, None);

    let completed = pane.process_pty_bytes(pane_id, 0, b"repo\x07", &tx);
    #[cfg(not(windows))]
    assert_eq!(
        completed.reported_cwd,
        Some(std::path::PathBuf::from("/tmp/gterm repo"))
    );
    #[cfg(windows)]
    assert_eq!(
        completed.reported_cwd,
        Some(std::path::PathBuf::from("\\tmp\\gterm repo"))
    );

    let latest = pane.process_pty_bytes(
        pane_id,
        0,
        b"\x1b]9;9;/tmp/conemu\x1b\\\x1b]1337;CurrentDir=/tmp/iterm2\x1b\\",
        &tx,
    );
    assert_eq!(
        latest.reported_cwd,
        Some(std::path::PathBuf::from("/tmp/iterm2"))
    );
}

#[test]
fn process_pty_bytes_surfaces_clipboard_writes_without_other_results() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();

    let result = pane.process_pty_bytes(
        PaneId::from_raw(1),
        0,
        b"output\x1b]52;c;Y2xpcGJvYXJk\x07",
        &tx,
    );

    assert!(result.request_render);
    assert_eq!(result.render_delay, None);
    assert_eq!(result.clipboard_writes, vec![b"clipboard".to_vec()]);
    assert_eq!(result.reported_cwd, None);
    assert!(result.terminal_responses.is_empty());
}

#[test]
fn seeded_history_clipboard_write_does_not_leak_into_live_output() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    pane.seed_history_ansi("\x1b]52;c;c3RhbGU=\x07");

    let result = pane.process_pty_bytes(PaneId::from_raw(1), 0, b"live output", &tx);

    assert!(result.clipboard_writes.is_empty());
}

#[test]
fn seeded_history_pwd_does_not_leak_into_live_output() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    pane.seed_history_ansi("\x1b]7;file:///tmp/restored\x07");

    let result = pane.process_pty_bytes(PaneId::from_raw(1), 0, b"live output", &tx);

    assert_eq!(result.reported_cwd, None);
}

#[cfg(windows)]
#[test]
fn windows_powershell_prompt_cwd_uses_latest_default_prompt_path() {
    let cwd = std::env::current_dir().unwrap();
    let text = format!("PS C:\\old> cd {}\nPS {}>", cwd.display(), cwd.display());

    assert_eq!(windows_powershell_prompt_cwd(&text).as_ref(), Some(&cwd));
}

#[cfg(windows)]
#[test]
fn windows_powershell_prompt_cwd_requires_current_prompt_line() {
    let cwd = std::env::current_dir().unwrap();
    let text = format!("PS {}>\ncommand output", cwd.display());

    assert_eq!(windows_powershell_prompt_cwd(&text), None);
}

#[cfg(windows)]
#[test]
fn windows_powershell_prompt_cwd_ignores_command_echo() {
    let cwd = std::env::current_dir().unwrap();
    let text = format!("PS {}> echo hi", cwd.display());

    assert_eq!(windows_powershell_prompt_cwd(&text), None);
}

#[cfg(windows)]
fn process_windows_powershell_prompt_bytes(
    bytes: &[u8],
    cols: u16,
    rows: u16,
    enabled: bool,
) -> ProcessBytesResult {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(cols, rows, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    pane.set_windows_powershell_prompt_cwd_reporting(enabled);
    pane.process_pty_bytes(PaneId::from_raw(1), 0, bytes, &tx)
}

#[cfg(windows)]
#[test]
fn process_pty_bytes_reports_windows_powershell_prompt_cwd() {
    let cwd = std::env::current_dir().unwrap();
    let bytes = format!("PS C:\\old> cd {}\r\nPS {}>", cwd.display(), cwd.display());

    let result = process_windows_powershell_prompt_bytes(bytes.as_bytes(), 80, 24, true);

    assert_eq!(result.reported_cwd.as_ref(), Some(&cwd));
}

#[cfg(windows)]
#[test]
fn process_pty_bytes_reports_wrapped_windows_powershell_prompt_cwd() {
    let cwd = std::env::current_dir().unwrap();
    let bytes = format!("PS {}>", cwd.display());

    let result = process_windows_powershell_prompt_bytes(bytes.as_bytes(), 12, 8, true);

    assert_eq!(result.reported_cwd.as_ref(), Some(&cwd));
}

#[cfg(windows)]
#[test]
fn process_pty_bytes_ignores_prompt_like_output_on_previous_line() {
    let cwd = std::env::current_dir().unwrap();
    let bytes = format!("PS {}>\r\n", cwd.display());

    let result = process_windows_powershell_prompt_bytes(bytes.as_bytes(), 80, 24, true);

    assert_eq!(result.reported_cwd, None);
}

#[cfg(windows)]
#[test]
fn process_pty_bytes_ignores_windows_powershell_prompt_cwd_on_alternate_screen() {
    let cwd = std::env::current_dir().unwrap();
    let bytes = format!("\x1b[?1049hPS {}>", cwd.display());

    let result = process_windows_powershell_prompt_bytes(bytes.as_bytes(), 80, 24, true);

    assert_eq!(result.reported_cwd, None);
}

#[cfg(windows)]
#[test]
fn process_pty_bytes_skips_windows_powershell_prompt_cwd_when_disabled() {
    let cwd = std::env::current_dir().unwrap();
    let bytes = format!("PS {}>", cwd.display());

    let result = process_windows_powershell_prompt_bytes(bytes.as_bytes(), 80, 24, false);

    assert_eq!(result.reported_cwd, None);
}

fn expected_xtgettcap_response(cap_hex: &str, value: Option<&[u8]>) -> Bytes {
    let mut response = format!("\x1bP1+r{cap_hex}").into_bytes();
    if let Some(value) = value {
        response.push(b'=');
        append_upper_hex(value, &mut response);
    }
    response.extend_from_slice(b"\x1b\\");
    Bytes::from(response)
}

fn append_upper_hex(bytes: &[u8], output: &mut Vec<u8>) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for &byte in bytes {
        output.push(HEX[usize::from(byte >> 4)]);
        output.push(HEX[usize::from(byte & 0x0f)]);
    }
}

#[test]
fn decscusr_cursor_shape_preserves_blinking_variants() {
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Block, true),
        1
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Block, false),
        2
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Underline, true),
        3
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Underline, false),
        4
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Bar, true),
        5
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::Bar, false),
        6
    );
    assert_eq!(
        decscusr_cursor_shape(crate::ghostty::CursorVisualStyle::BlockHollow, false),
        2
    );
}

#[test]
fn cursor_state_uses_terminal_default_until_child_sets_shape() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    assert_eq!(pane.cursor_state().unwrap().shape, 0);

    pane.process_pty_bytes(pane_id, 0, b"\x1b[6 q", &tx);

    assert_eq!(pane.cursor_state().unwrap().shape, 6);
}

#[test]
fn cursor_state_returns_terminal_default_after_decscusr_reset() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"\x1b[2 q", &tx);
    assert_eq!(pane.cursor_state().unwrap().shape, 2);

    pane.process_pty_bytes(pane_id, 0, b"\x1b[0 q", &tx);

    assert_eq!(pane.cursor_state().unwrap().shape, 0);
}

#[test]
fn cursor_shape_tracker_handles_split_decscusr_sequences() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"\x1b[", &tx);
    pane.process_pty_bytes(pane_id, 0, b"5 ", &tx);
    pane.process_pty_bytes(pane_id, 0, b"q", &tx);

    assert_eq!(pane.cursor_state().unwrap().shape, 5);
}

#[test]
#[cfg(windows)]
fn cursor_state_holds_pty_position_change_until_settle_window() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"x", &tx);
    assert_eq!(
        pane.cursor_state()
            .map(|cursor| (cursor.x, cursor.y, cursor.visible)),
        Some((1, 0, true))
    );

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[6;21H", &tx);

    assert_eq!(result.render_delay, Some(CURSOR_POSITION_SETTLE));
    assert_eq!(
        pane.cursor_state()
            .map(|cursor| (cursor.x, cursor.y, cursor.visible)),
        Some((1, 0, true))
    );
}

#[test]
#[cfg(not(windows))]
fn cursor_state_uses_live_position_when_settle_policy_disabled() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"x", &tx);
    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[6;21H", &tx);

    assert_eq!(result.render_delay, None);
    assert_eq!(
        pane.cursor_state()
            .map(|cursor| (cursor.x, cursor.y, cursor.visible)),
        Some((20, 5, true))
    );
}

#[test]
fn cursor_settle_policy_controls_render_delay() {
    assert_eq!(
        render_delay_after_pty_write(false, false, true, true),
        Some(CURSOR_POSITION_SETTLE)
    );
    assert_eq!(
        render_delay_after_pty_write(false, false, true, false),
        None
    );
    assert_eq!(
        render_delay_after_pty_write(false, true, true, false),
        Some(KITTY_GRAPHICS_REDRAW_SETTLE)
    );
    assert_eq!(render_delay_after_pty_write(true, false, true, true), None);
}

#[test]
fn host_terminal_theme_restore_probe_skips_when_no_transient_override() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let core = pane.core.lock().unwrap();

    assert!(!should_probe_host_terminal_theme_restore(&core));
}

#[test]
fn host_terminal_theme_restore_probe_skips_when_host_theme_unknown() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.transient_default_color_owner_pgid = Some(42);
    }
    let core = pane.core.lock().unwrap();

    assert!(!should_probe_host_terminal_theme_restore(&core));
}

#[test]
fn host_terminal_theme_restore_probe_skips_on_alternate_screen() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1049h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.transient_default_color_owner_pgid = Some(42);
        core.host_terminal_theme = crate::terminal_theme::TerminalTheme {
            foreground: Some(crate::terminal_theme::RgbColor {
                r: 0xaa,
                g: 0xbb,
                b: 0xcc,
            }),
            background: Some(crate::terminal_theme::RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            }),
            ..Default::default()
        };
    }
    let core = pane.core.lock().unwrap();

    assert!(!should_probe_host_terminal_theme_restore(&core));
}

#[test]
fn host_terminal_theme_restore_probe_runs_when_restore_is_pending() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.transient_default_color_owner_pgid = Some(42);
        core.host_terminal_theme = crate::terminal_theme::TerminalTheme {
            foreground: Some(crate::terminal_theme::RgbColor {
                r: 0xaa,
                g: 0xbb,
                b: 0xcc,
            }),
            background: Some(crate::terminal_theme::RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            }),
            ..Default::default()
        };
    }
    let core = pane.core.lock().unwrap();

    assert!(should_probe_host_terminal_theme_restore(&core));
}

#[test]
fn ghostty_render_can_suppress_cursor_position() {
    let (tx, _rx) = mpsc::channel(4);
    let mut first_terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    first_terminal.write(b"left");
    let first = GhosttyPaneTerminal::new(first_terminal, tx.clone()).unwrap();

    let mut second_terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    second_terminal.write(b"r\r\nb");
    let second = GhosttyPaneTerminal::new(second_terminal, tx).unwrap();

    let backend = ratatui::backend::TestBackend::new(40, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| {
            first.render(frame, Rect::new(0, 0, 20, 5), true);
            second.render(frame, Rect::new(20, 0, 20, 5), false);
        })
        .unwrap();

    terminal.backend_mut().assert_cursor_position((4, 0));
}

#[test]
fn ghostty_keyboard_protocol_tracks_live_terminal_flags() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[>3u");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert_eq!(
        pane.keyboard_protocol(),
        Some(crate::input::KeyboardProtocol::Kitty { flags: 3 })
    );
}

#[test]
fn ghostty_plain_text_chars_still_encode_as_text() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Char('a'),
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );

    assert_eq!(encoded, b"a");
}

#[test]
fn ghostty_enter_backspace_release_in_legacy_pane_emits_nothing() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    for code in [
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyCode::Backspace,
    ] {
        let press = pane.encode_terminal_key(
            crate::input::TerminalKey::new(code, crossterm::event::KeyModifiers::empty()),
            crate::input::KeyboardProtocol::Legacy,
        );
        let release = pane.encode_terminal_key(
            crate::input::TerminalKey::new(code, crossterm::event::KeyModifiers::empty())
                .with_kind(crossterm::event::KeyEventKind::Release),
            crate::input::KeyboardProtocol::Legacy,
        );
        assert!(!press.is_empty(), "{code:?} press should emit bytes");
        assert!(
            release.is_empty(),
            "{code:?} release should emit nothing in a legacy pane, got {release:?}"
        );
    }
}

#[test]
fn ghostty_report_event_pane_keeps_basic_compatibility_keys_legacy() {
    let (tx, _rx) = mpsc::channel(4);
    // Push kitty flags including REPORT_EVENT_TYPES (0b10) + DISAMBIGUATE (0b1).
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[>3u");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    for (code, expected) in [
        (crossterm::event::KeyCode::Enter, b"\r".as_slice()),
        (crossterm::event::KeyCode::Backspace, b"\x7f".as_slice()),
    ] {
        let press = pane.encode_terminal_key(
            crate::input::TerminalKey::new(code, crossterm::event::KeyModifiers::empty()),
            pane.keyboard_protocol().unwrap(),
        );
        assert_eq!(
            press, expected,
            "{code:?} press should stay legacy-compatible without REPORT_ALL_KEYS"
        );

        let release = pane.encode_terminal_key(
            crate::input::TerminalKey::new(code, crossterm::event::KeyModifiers::empty())
                .with_kind(crossterm::event::KeyEventKind::Release),
            pane.keyboard_protocol().unwrap(),
        );
        assert!(
            release.is_empty(),
            "{code:?} release should not fall back to legacy bytes, got {release:?}"
        );
    }
}

#[test]
fn ghostty_char_keys_still_use_gterm_encoding() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[>1u");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Char('a'),
            crossterm::event::KeyModifiers::CONTROL | crossterm::event::KeyModifiers::SHIFT,
        ),
        crate::input::KeyboardProtocol::Legacy,
    );

    assert_eq!(encoded, vec![1]);
}

#[test]
fn ghostty_key_encoding_honors_application_cursor_mode() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal
        .mode_set(crate::ghostty::MODE_APPLICATION_CURSOR_KEYS, true)
        .unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );

    assert_eq!(encoded, b"\x1bOA");
}

#[cfg(unix)]
#[test]
fn ghostty_seed_handoff_input_state_restores_input_modes() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    pane.seed_handoff_input_state(InputState {
        alternate_screen: true,
        application_cursor: true,
        bracketed_paste: true,
        focus_reporting: true,
        mouse_protocol_mode: crate::input::MouseProtocolMode::ButtonMotion,
        mouse_protocol_encoding: crate::input::MouseProtocolEncoding::Sgr,
        mouse_alternate_scroll: true,
        modify_other_keys: true,
        color_scheme_reporting: true,
    });

    assert_eq!(
        pane.input_state(),
        Some(InputState {
            alternate_screen: true,
            application_cursor: true,
            bracketed_paste: true,
            focus_reporting: true,
            mouse_protocol_mode: crate::input::MouseProtocolMode::ButtonMotion,
            mouse_protocol_encoding: crate::input::MouseProtocolEncoding::Sgr,
            mouse_alternate_scroll: true,
            modify_other_keys: true,
            color_scheme_reporting: true,
        })
    );

    let encoded = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );
    assert_eq!(encoded, b"\x1bOA");

    let key = crate::input::parse_terminal_key_sequence("\x1b[13;2u").unwrap();
    let encoded = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);
    assert_eq!(encoded, b"\x1b[27;2;13~");
}

#[test]
fn grouped_semantic_key_repeats_expand_at_the_destination() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let key = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Char('x'),
        crossterm::event::KeyModifiers::empty(),
    )
    .with_repeat_count(3);

    assert_eq!(
        pane.encode_terminal_key(key, crate::input::KeyboardProtocol::Legacy),
        b"xxx"
    );
}

#[test]
fn grouped_release_is_encoded_once() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[>11u");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let protocol = pane.keyboard_protocol().unwrap();
    let release = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Up,
        crossterm::event::KeyModifiers::empty(),
    )
    .with_kind(crossterm::event::KeyEventKind::Release);
    let expected = pane.encode_terminal_key(release.clone(), protocol);

    assert!(!expected.is_empty());
    let mut malformed_release = release;
    malformed_release.repeat_count = 3;
    assert_eq!(
        pane.encode_terminal_key(malformed_release, protocol),
        expected
    );
}

#[test]
fn ghostty_key_encoder_updates_after_terminal_mode_changes() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let before = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );
    assert_eq!(before, b"\x1b[A");

    pane.process_pty_bytes(pane_id, 0, b"\x1b[?1h", &tx);

    let after = pane.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );
    assert_eq!(after, b"\x1bOA");
}

#[test]
fn ghostty_key_encoder_updates_after_kitty_flag_changes() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    let key = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Enter,
        crossterm::event::KeyModifiers::CONTROL | crossterm::event::KeyModifiers::SHIFT,
    );

    let before = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);
    pane.process_pty_bytes(pane_id, 0, b"\x1b[>1u", &tx);
    let after = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);

    assert_ne!(before, after);
    assert_eq!(after, b"\x1b[13;6u");
}

#[test]
fn ghostty_kitty_pane_encodes_shift_enter_as_csi_u() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.process_pty_bytes(pane_id, 0, b"\x1b[>5u", &tx);

    let key = crate::input::parse_terminal_key_sequence("\x1b[13;2u").unwrap();
    let encoded = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);

    assert_eq!(
        pane.keyboard_protocol(),
        Some(crate::input::KeyboardProtocol::Kitty { flags: 5 })
    );
    assert_eq!(encoded, b"\x1b[13;2u");
}

#[cfg(unix)]
#[test]
fn ghostty_seed_keyboard_protocol_flags_restores_shift_enter_encoding() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    pane.seed_keyboard_protocol_flags(5);

    let key = crate::input::parse_terminal_key_sequence("\x1b[13;2u").unwrap();
    let encoded = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);

    assert_eq!(
        pane.keyboard_protocol(),
        Some(crate::input::KeyboardProtocol::Kitty { flags: 5 })
    );
    assert_eq!(encoded, b"\x1b[13;2u");
}

#[cfg(unix)]
#[test]
fn ghostty_keyboard_protocol_state_replays_nested_stack() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.process_pty_bytes(pane_id, 0, b"\x1b[>1u\x1b[>5u", &tx);

    let ansi = pane.kitty_keyboard_state_ansi().unwrap();

    let (restored_tx, _restored_rx) = mpsc::channel(4);
    let restored_terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let restored = GhosttyPaneTerminal::new(restored_terminal, restored_tx).unwrap();
    restored.seed_keyboard_protocol_ansi(&ansi);
    assert_eq!(
        restored.keyboard_protocol(),
        Some(crate::input::KeyboardProtocol::Kitty { flags: 5 })
    );

    let (pop_tx, _pop_rx) = mpsc::channel(4);
    restored.process_pty_bytes(pane_id, 0, b"\x1b[<u", &pop_tx);
    assert_eq!(
        restored.keyboard_protocol(),
        Some(crate::input::KeyboardProtocol::Kitty { flags: 1 })
    );
}

#[cfg(windows)]
#[test]
fn windows_ghostty_default_pane_preserves_synthesized_shift_enter_fallback() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let key = crate::input::parse_terminal_key_sequence("\x1b[13;2u").unwrap();

    assert_eq!(
        pane.encode_terminal_key(key, crate::input::KeyboardProtocol::Legacy),
        b"\x1b[13;28;13;1;16;1_"
    );
}

#[test]
fn ghostty_modify_other_keys_mode_one_preserves_shift_enter() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let key = crate::input::parse_terminal_key_sequence("\x1b[13;2u").unwrap();

    pane.seed_history_ansi("\x1b[>4;1m");
    let encoded = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);

    assert_eq!(encoded, b"\x1b[27;2;13~");
}

#[test]
fn ghostty_kitty_pane_encodes_parsed_legacy_alt_backspace_as_csi_u() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.process_pty_bytes(pane_id, 0, b"\x1b[>1u", &tx);

    let key = crate::input::parse_terminal_key_sequence("\x1b\x7f").unwrap();
    let encoded = pane.encode_terminal_key(key.clone(), crate::input::KeyboardProtocol::Legacy);

    assert_eq!(encoded, b"\x1b[127;3u");
}

#[test]
fn ghostty_pane_characterizes_ctrl_backspace_encoding() {
    let (tx, _rx) = mpsc::channel(4);
    let legacy = GhosttyPaneTerminal::new(
        crate::ghostty::Terminal::new(80, 24, 0).unwrap(),
        tx.clone(),
    )
    .unwrap();

    let ctrl_backspace = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Backspace,
        crossterm::event::KeyModifiers::CONTROL,
    );
    assert_eq!(
        legacy.encode_terminal_key(
            ctrl_backspace.clone(),
            crate::input::KeyboardProtocol::Legacy
        ),
        b"\x08"
    );

    let plain_backspace = crate::input::TerminalKey::new(
        crossterm::event::KeyCode::Backspace,
        crossterm::event::KeyModifiers::empty(),
    );
    assert_eq!(
        legacy.encode_terminal_key(plain_backspace, crate::input::KeyboardProtocol::Legacy),
        b"\x7f"
    );

    let kitty = GhosttyPaneTerminal::new(
        crate::ghostty::Terminal::new(80, 24, 0).unwrap(),
        tx.clone(),
    )
    .unwrap();
    let pane_id = PaneId::from_raw(1);
    kitty.process_pty_bytes(pane_id, 0, b"\x1b[>1u", &tx);

    assert_eq!(
        kitty.encode_terminal_key(ctrl_backspace, crate::input::KeyboardProtocol::Legacy),
        b"\x1b[127;5u"
    );
}

#[test]
fn ghostty_key_encoders_are_isolated_per_pane() {
    let (tx, _rx) = mpsc::channel(4);
    let first = GhosttyPaneTerminal::new(
        crate::ghostty::Terminal::new(80, 24, 0).unwrap(),
        tx.clone(),
    )
    .unwrap();
    let second = GhosttyPaneTerminal::new(
        crate::ghostty::Terminal::new(80, 24, 0).unwrap(),
        tx.clone(),
    )
    .unwrap();

    first.process_pty_bytes(PaneId::from_raw(1), 0, b"\x1b[?1h", &tx);

    let first_encoded = first.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );
    let second_encoded = second.encode_terminal_key(
        crate::input::TerminalKey::new(
            crossterm::event::KeyCode::Up,
            crossterm::event::KeyModifiers::empty(),
        ),
        crate::input::KeyboardProtocol::Legacy,
    );

    assert_eq!(first_encoded, b"\x1bOA");
    assert_eq!(second_encoded, b"\x1b[A");
}

#[test]
fn ghostty_mouse_button_encoding_uses_live_terminal_state() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1000h\x1b[?1006h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_mouse_button(
        crossterm::event::MouseEventKind::Up(crossterm::event::MouseButton::Left),
        11,
        9,
        crossterm::event::KeyModifiers::empty(),
    );

    assert_eq!(encoded.as_deref(), Some(&b"\x1b[<0;12;10m"[..]));
}

#[test]
fn ghostty_mouse_drag_encoding_uses_motion_reporting_state() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1002h\x1b[?1006h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_mouse_button(
        crossterm::event::MouseEventKind::Drag(crossterm::event::MouseButton::Left),
        4,
        6,
        crossterm::event::KeyModifiers::SHIFT,
    );

    assert_eq!(encoded.as_deref(), Some(&b"\x1b[<36;5;7M"[..]));
}

#[test]
fn ghostty_mouse_drag_without_motion_reporting_is_not_forwarded() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1000h\x1b[?1006h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_mouse_button(
        crossterm::event::MouseEventKind::Drag(crossterm::event::MouseButton::Left),
        4,
        6,
        crossterm::event::KeyModifiers::empty(),
    );

    assert_eq!(encoded, None);
}

#[test]
fn ghostty_mouse_moved_encoding_uses_any_motion_state() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1003h\x1b[?1006h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_mouse_motion(
        crossterm::event::MouseEventKind::Moved,
        4,
        6,
        crossterm::event::KeyModifiers::empty(),
    );

    assert_eq!(encoded.as_deref(), Some(&b"\x1b[<35;5;7M"[..]));
}

#[test]
fn ghostty_mouse_sgr_pixels_downgrades_to_cell_coordinates() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.write(b"\x1b[?1003h\x1b[?1006h\x1b[?1016h");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let encoded = pane.encode_mouse_motion(
        crossterm::event::MouseEventKind::Moved,
        4,
        6,
        crossterm::event::KeyModifiers::empty(),
    );

    assert_eq!(encoded.as_deref(), Some(&b"\x1b[<35;5;7M"[..]));
}

#[test]
fn ghostty_normalize_buffer_symbol_prefers_grapheme_width_when_metadata_disagrees() {
    const WIDE_GRAPHEME: &str = "🙂";
    const FLAG_GRAPHEME: &str = "🇧🇷";
    const FAMILY_GRAPHEME: &str = "👨‍👩‍👧";
    const VS16_GRAPHEME: &str = "⚠️";
    const EMOJI_GRAPHEME: &str = "💳";

    assert_eq!(
        ghostty_normalize_buffer_symbol(WIDE_GRAPHEME, crate::ghostty::CellWide::Wide),
        WIDE_GRAPHEME
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol("a", crate::ghostty::CellWide::Wide),
        "  "
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol(FLAG_GRAPHEME, crate::ghostty::CellWide::Wide),
        FLAG_GRAPHEME
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol(FAMILY_GRAPHEME, crate::ghostty::CellWide::Wide),
        FAMILY_GRAPHEME
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol("⌨️", crate::ghostty::CellWide::Narrow),
        "⌨️"
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol(VS16_GRAPHEME, crate::ghostty::CellWide::Narrow),
        VS16_GRAPHEME
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol(EMOJI_GRAPHEME, crate::ghostty::CellWide::Narrow),
        EMOJI_GRAPHEME
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol(" ", crate::ghostty::CellWide::SpacerTail),
        ""
    );
    assert_eq!(
        ghostty_normalize_buffer_symbol("xx", crate::ghostty::CellWide::SpacerHead),
        " "
    );
}

fn render_cells_to_symbols(
    terminal: &mut crate::ghostty::Terminal,
) -> Vec<(crate::ghostty::CellWide, String)> {
    let mut render_state = crate::ghostty::RenderState::new().unwrap();
    render_state.update(terminal).unwrap();

    let mut row_iterator = crate::ghostty::RowIterator::new().unwrap();
    let mut rows = render_state
        .populate_row_iterator(&mut row_iterator)
        .unwrap();
    let mut row_cells = crate::ghostty::RowCells::new().unwrap();
    let mut grapheme_bytes = Vec::new();
    let mut symbol_scratch = String::new();
    let mut out = Vec::new();

    if rows.next() {
        let mut cells = rows.populate_cells(&mut row_cells).unwrap();
        while cells.next() {
            let wide = cells.wide().unwrap_or(crate::ghostty::CellWide::Narrow);
            let symbol = ghostty_buffer_symbol_into(
                &cells,
                wide,
                false,
                &mut grapheme_bytes,
                &mut symbol_scratch,
            )
            .unwrap()
            .to_string();
            out.push((wide, symbol));
        }
    }

    out
}

#[test]
fn grapheme_cluster_mode_renders_flag_emoji_in_single_wide_cell() {
    let mut terminal = crate::ghostty::Terminal::new(40, 1, 0).unwrap();
    terminal.write("🇧🇷".as_bytes());

    let cells = render_cells_to_symbols(&mut terminal);

    assert!(
        cells
            .iter()
            .any(|(wide, symbol)| *wide == crate::ghostty::CellWide::Wide && symbol == "🇧🇷"),
        "expected a wide cell containing the full flag grapheme, got {cells:?}"
    );
}

#[test]
fn grapheme_cluster_mode_renders_zwj_family_in_single_wide_cell() {
    let mut terminal = crate::ghostty::Terminal::new(40, 1, 0).unwrap();
    terminal.write("👨\u{200d}👩\u{200d}👧".as_bytes());

    let cells = render_cells_to_symbols(&mut terminal);

    assert!(
        cells
            .iter()
            .any(|(wide, symbol)| *wide == crate::ghostty::CellWide::Wide
                && symbol == "👨\u{200d}👩\u{200d}👧"),
        "expected a wide cell containing the full ZWJ grapheme, got {cells:?}"
    );
}

#[test]
fn pane_scrollback_controls_round_trip_and_clamp_without_ui_interference() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 3, 100).unwrap();
    write_numbered_lines(&mut terminal, 1000);
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let before = pane.scroll_metrics().expect("scroll metrics before scroll");
    assert!(before.max_offset_from_bottom > 0);
    assert_eq!(before.offset_from_bottom, 0);

    for offset in [
        0,
        before.max_offset_from_bottom / 2,
        before.max_offset_from_bottom,
        usize::MAX,
    ] {
        pane.set_scroll_offset_from_bottom(offset);
        let after = pane.scroll_metrics().expect("scroll metrics after scroll");
        assert_eq!(
            after.offset_from_bottom,
            offset.min(after.max_offset_from_bottom)
        );
    }

    assert!(pane.visible_text().contains("000000"));
}

#[test]
fn empty_or_short_resize_keeps_following_bottom_when_output_creates_scrollback() {
    for initial in [b"".as_slice(), b"seed\r\n".as_slice()] {
        let (tx, _rx) = mpsc::channel(4);
        let mut terminal = crate::ghostty::Terminal::new(10, 3, 100).unwrap();
        terminal.write(initial);
        let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
        let pane_id = PaneId::from_raw(1);

        pane.resize(3, 10, 0, 0);
        pane.process_pty_bytes(
            pane_id,
            0,
            b"000000\r\n000001\r\n000002\r\n000003\r\n000004",
            &tx,
        );

        let metrics = pane.scroll_metrics().expect("scroll metrics after output");
        assert_eq!(metrics.offset_from_bottom, 0);
        assert!(pane.visible_text().contains("000004"));
    }
}

#[test]
fn resize_that_removes_scrollback_restores_live_follow() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(10, 3, 100).unwrap();
    terminal.write(b"000000\r\n000001\r\n000002\r\n000003\r\n000004");
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.set_scroll_offset_from_bottom(1);
    pane.resize(5, 10, 0, 0);
    let resized = pane.scroll_metrics().expect("scroll metrics after resize");
    assert_eq!(resized.max_offset_from_bottom, 0);

    pane.process_pty_bytes(pane_id, 0, b"\r\n000005\r\n000006", &tx);

    let metrics = pane.scroll_metrics().expect("scroll metrics after output");
    assert_eq!(metrics.offset_from_bottom, 0);
    assert!(pane.visible_text().contains("000006"));
}

#[test]
fn detection_text_stays_at_bottom_when_viewport_is_scrolled() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 3, 100).unwrap();
    write_numbered_lines(&mut terminal, 10);
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let bottom_snapshot = pane.detection_text();
    assert_eq!(bottom_snapshot, pane.recent_text(3));
    assert!(bottom_snapshot.contains("000009"));

    let before = pane.scroll_metrics().expect("scroll metrics before scroll");
    pane.set_scroll_offset_from_bottom(before.max_offset_from_bottom);

    assert!(pane.visible_text().contains("000000"));
    assert_eq!(pane.detection_text(), bottom_snapshot);
}

#[test]
fn extract_selection_reads_screen_rows_not_current_viewport() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(8, 3, 1024).unwrap();
    write_numbered_lines(&mut terminal, 8);
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    pane.set_scroll_offset_from_bottom(3);
    let metrics = pane
        .scroll_metrics()
        .expect("scroll metrics after initial scroll");
    let mut selection =
        crate::selection::Selection::anchor(PaneId::from_raw(1), 0, 0, Some(metrics));
    selection.drag(5, 2, Rect::new(0, 0, 8, 3), Some(metrics));

    pane.scroll_reset();

    let text = pane
        .extract_selection(&selection)
        .expect("selection should extract text");
    assert_eq!(text, "000003\n000004\n000005");
}

#[test]
fn recent_unwrapped_text_ignores_soft_wraps() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(5, 3, 100).unwrap();
    terminal.write(b"ABCDEFGHIJ");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert_eq!(pane.recent_text(3), "ABCDE\nFGHIJ\n");
    assert_eq!(pane.recent_unwrapped_text(3), "ABCDEFGHIJ");
}

#[test]
fn recent_snapshots_report_omitted_rendered_rows() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 100).unwrap();
    terminal.write(b"one\r\ntwo\r\nthree\r\nfour");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert!(pane.recent_text_snapshot(2).truncated);
    assert!(pane.recent_ansi_snapshot(2).truncated);
    assert!(pane.recent_unwrapped_text_snapshot(2).truncated);
    assert!(pane.recent_unwrapped_ansi_snapshot(2).truncated);
    assert!(!pane.recent_text_snapshot(100).truncated);
}

#[test]
fn plain_text_reads_skip_wide_character_spacer_cells() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(40, 3, 100).unwrap();
    terminal.write("日本語テスト ABC 123".as_bytes());
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert_eq!(pane.visible_text(), "日本語テスト ABC 123\n");
    assert_eq!(pane.recent_text(3), "日本語テスト ABC 123\n");
    assert_eq!(pane.recent_unwrapped_text(3), "日本語テスト ABC 123");
    assert_eq!(pane.detection_text(), "日本語テスト ABC 123\n");
}

#[test]
fn visible_ansi_preserves_cell_style_sequences() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 100).unwrap();
    terminal.write(b"\x1b[31;1mred\x1b[0m plain");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let ansi = pane.visible_ansi();
    assert!(ansi.contains("red"));
    assert!(ansi.contains("plain"));
    assert!(ansi.contains("\x1b["));
}

#[test]
fn recent_ansi_can_read_styled_scrollback() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 100).unwrap();
    terminal.write(b"\x1b[34mblue\x1b[0m\r\nline2\r\nline3\r\nline4");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let ansi = pane.recent_ansi(4);
    assert!(ansi.contains("blue"));
    assert!(ansi.contains("line4"));
    assert!(ansi.contains("\x1b["));
}

#[test]
fn resize_shrinks_both_axes_with_cursor_at_old_bottom() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(8, 4, 10_000).unwrap();
    terminal.write(b"alpha\r\nbeta\r\ngamma\r\ndelta");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    pane.resize(3, 7, 8, 16);

    assert_eq!(pane.visible_text(), "beta\ngamma\ndelta\n");
    assert_eq!(pane.detection_text(), "beta\ngamma\ndelta\n");
    assert_eq!(
        pane.scroll_metrics(),
        Some(ScrollMetrics {
            offset_from_bottom: 0,
            max_offset_from_bottom: 1,
            viewport_rows: 3,
        })
    );
}

#[test]
fn resize_reflow_keeps_scrolled_viewport_and_bottom_detection_sane() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(12, 4, 10_000).unwrap();
    write_wrapped_contract_lines(&mut terminal, 40);
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let bottom_snapshot = pane.detection_text();
    assert!(bottom_snapshot.contains("END"));

    let initial = pane.scroll_metrics().expect("initial scroll metrics");
    assert!(initial.max_offset_from_bottom > 0);
    pane.set_scroll_offset_from_bottom(initial.max_offset_from_bottom / 2);
    assert!(!pane.visible_text().trim().is_empty());

    for (rows, cols) in [(4, 10), (4, 7), (6, 18), (3, 9), (5, 12)] {
        let before_resize = pane.scroll_metrics().expect("scroll metrics before resize");
        pane.resize(rows, cols, 0, 0);

        let metrics = pane.scroll_metrics().expect("scroll metrics after resize");
        assert_eq!(metrics.viewport_rows, rows as usize);
        assert_eq!(
            metrics.offset_from_bottom,
            before_resize
                .offset_from_bottom
                .min(metrics.max_offset_from_bottom)
        );
        assert!(
            metrics.offset_from_bottom > 0,
            "resize should preserve a scrolled viewport instead of jumping to bottom"
        );
        assert!(metrics.max_offset_from_bottom > 0);
        let visible = pane.visible_text();
        assert!(
                !visible.trim().is_empty(),
                "visible text should not be empty after resize to {rows}x{cols}; metrics={metrics:?}; detection={:?}; recent={:?}",
                pane.detection_text(),
                pane.recent_text(6)
            );
        assert!(
            pane.detection_text().contains("END"),
            "bottom detection should remain independent from the scrolled viewport after resize"
        );
    }
}

#[test]
fn resize_recovery_does_not_replay_history_when_visible_screen_was_blank() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 10_000).unwrap();
    terminal.write(b"old history\r\n\x1b[2J\x1b[H");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert!(pane.visible_text().trim().is_empty());
    assert!(pane.detection_text().trim().is_empty());

    pane.resize(3, 20, 0, 0);

    assert!(pane.visible_text().trim().is_empty());
    assert!(pane.detection_text().trim().is_empty());
    assert!(pane.recent_text(3).trim().is_empty());
}

#[test]
fn resize_recovery_does_not_replay_scrolled_history_over_blank_bottom() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(20, 3, 10_000).unwrap();
    write_numbered_lines(&mut terminal, 20);
    terminal.write(b"\x1b[2J\x1b[H");
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    assert!(pane.detection_text().trim().is_empty());
    let metrics = pane.scroll_metrics().expect("scroll metrics");
    pane.set_scroll_offset_from_bottom(metrics.max_offset_from_bottom);
    assert!(!pane.visible_text().trim().is_empty());

    pane.resize(3, 20, 0, 0);

    assert!(pane.detection_text().trim().is_empty());
    assert!(pane.recent_text(3).trim().is_empty());
}

#[test]
fn process_pty_bytes_answers_xtwinops_size_queries() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.resize(24, 80, 9, 18);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[14t\x1b[16t\x1b[18t", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![
            Bytes::from_static(b"\x1b[4;432;720t"),
            Bytes::from_static(b"\x1b[6;18;9t"),
            Bytes::from_static(b"\x1b[8;24;80t"),
        ]
    );
}

#[test]
fn xtwinops_size_queries_follow_successful_resize() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.resize(24, 80, 9, 18);
    pane.resize(30, 100, 10, 20);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[14t\x1b[16t\x1b[18t", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![
            Bytes::from_static(b"\x1b[4;600;1000t"),
            Bytes::from_static(b"\x1b[6;20;10t"),
            Bytes::from_static(b"\x1b[8;30;100t"),
        ]
    );
}

#[test]
fn xtwinops_size_queries_stay_silent_without_pixel_geometry() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    for (cell_width_px, cell_height_px) in [(0, 0), (0, 18), (9, 0)] {
        pane.resize(24, 80, cell_width_px, cell_height_px);
        let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[14t\x1b[16t\x1b[18t", &tx);
        assert!(result.terminal_responses.is_empty());
    }
}

#[test]
fn resize_returns_in_band_size_report_response() {
    let (tx, _rx) = mpsc::channel(4);
    let mut terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    terminal.mode_set(2048, true).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    let responses = pane.resize(40, 100, 9, 18);

    assert_eq!(
        responses,
        vec![Bytes::from_static(b"\x1B[48;40;100;720;900t")]
    );
}

#[test]
fn synchronized_output_suppresses_intermediate_render_requests_until_batch_ends() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane_terminal = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let begin = pane_terminal.process_pty_bytes(pane_id, 0, b"\x1b[?2026h", &tx);
    assert!(!begin.request_render);

    let body = pane_terminal.process_pty_bytes(pane_id, 0, b"hello", &tx);
    assert!(!body.request_render);

    let end = pane_terminal.process_pty_bytes(pane_id, 0, b"\x1b[?2026l", &tx);
    assert!(end.request_render);
}

#[test]
fn kitty_graphics_write_requests_render_with_settle_backstop() {
    crate::kitty_graphics::set_enabled(true);
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(80, 24, 0).unwrap();
    let pane_terminal = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane_terminal.process_pty_bytes(
        pane_id,
        0,
        b"\x1b_Ga=T,f=32,t=d,i=7,p=1,s=1,v=1,q=2;/wAA/w==\x1b\\",
        &tx,
    );

    assert!(result.request_render);
    assert_eq!(result.render_delay, Some(KITTY_GRAPHICS_REDRAW_SETTLE));
}

#[test]
fn seeded_history_is_rendered_on_next_draw() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 100).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    pane.seed_history_ansi("restored history");

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let row = (0..16).map(|x| buffer[(x, 0)].symbol()).collect::<String>();
    assert_eq!(row, "restored history");
}

#[test]
fn render_leaves_unknown_host_default_background_transparent() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"hi");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().fg, Some(Color::Reset));
    assert_eq!(buffer[(0, 0)].style().bg, Some(Color::Reset));
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().fg, Some(Color::Reset));
    assert_eq!(buffer[(2, 0)].style().bg, Some(Color::Reset));
}

#[test]
fn render_blanks_kitty_unicode_placeholders_when_graphics_enabled() {
    crate::kitty_graphics::set_enabled(true);
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal
            .write("before\u{10eeee}\u{0305}\u{0305}after".as_bytes());
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();
    crate::kitty_graphics::set_enabled(false);

    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(0, 0)].symbol(), "b");
    assert_eq!(buffer[(6, 0)].symbol(), " ");
    assert_eq!(buffer[(7, 0)].symbol(), "a");
    assert_eq!(pane.visible_text().lines().next(), Some("before after"));
    assert_eq!(pane.recent_text(5), "before after\n");
}

#[test]
fn render_keeps_explicit_cell_foreground_when_host_is_unknown() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[38;2;68;85;102mhi\x1b[0m");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let expected_fg = Some(Color::Rgb(0x44, 0x55, 0x66));
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().fg, expected_fg);
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().fg, Some(Color::Reset));
}

#[test]
fn render_keeps_explicit_cell_background_when_host_is_unknown() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[48;2;68;85;102mhi\x1b[0m");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let expected_bg = Some(Color::Rgb(0x44, 0x55, 0x66));
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().bg, expected_bg);
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().bg, Some(Color::Reset));
}

#[test]
fn render_preserves_palette_colors_instead_of_flattening_to_rgb() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(
            b"\x1b[31mR\x1b[0m \x1b[38;5;171mI\x1b[0m \x1b[48;5;4mB\x1b[0m \x1b[38;2;1;2;3mT",
        );
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(0, 0)].symbol(), "R");
    assert_eq!(buffer[(0, 0)].style().fg, Some(Color::Indexed(1)));
    assert_eq!(buffer[(2, 0)].symbol(), "I");
    assert_eq!(buffer[(2, 0)].style().fg, Some(Color::Indexed(171)));
    assert_eq!(buffer[(4, 0)].symbol(), "B");
    assert_eq!(buffer[(4, 0)].style().bg, Some(Color::Indexed(4)));
    assert_eq!(buffer[(6, 0)].symbol(), "T");
    assert_eq!(buffer[(6, 0)].style().fg, Some(Color::Rgb(1, 2, 3)));
}

#[test]
fn render_preserves_palette_background_fill_cells() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[48;5;4m\x1b[K");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    for x in 0..20 {
        assert_eq!(buffer[(x, 0)].symbol(), " ");
        assert_eq!(buffer[(x, 0)].style().bg, Some(Color::Indexed(4)));
    }
}

#[test]
fn render_preserves_rgb_background_fill_cells() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[48;2;17;34;51m\x1b[K");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    for x in 0..20 {
        assert_eq!(buffer[(x, 0)].symbol(), " ");
        assert_eq!(buffer[(x, 0)].style().bg, Some(Color::Rgb(17, 34, 51)));
    }
}

#[test]
fn process_pty_bytes_does_not_advertise_unsupported_glyph_protocol() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b_25a1;s\x1b\\", &tx);

    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_libghostty_query_responses_without_queuing_input() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[6n", &tx);

    assert_eq!(result.terminal_responses.len(), 1);
    assert!(String::from_utf8_lossy(&result.terminal_responses[0]).contains('R'));
    assert!(rx.try_recv().is_err());
}

#[test]
fn color_scheme_queries_and_live_updates_follow_terminal_mode() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    assert!(pane
        .apply_host_terminal_appearance(Some(crate::terminal_theme::HostAppearance::Dark))
        .is_none());
    let query = pane.process_pty_bytes(pane_id, 0, b"\x1b[?996n", &tx);
    assert_eq!(
        query.terminal_responses,
        vec![Bytes::from_static(b"\x1b[?997;1n")]
    );

    pane.process_pty_bytes(pane_id, 0, b"\x1b[?2031h", &tx);
    assert!(pane
        .apply_host_terminal_appearance(Some(crate::terminal_theme::HostAppearance::Dark))
        .is_none());
    assert_eq!(
        pane.apply_host_terminal_appearance(Some(crate::terminal_theme::HostAppearance::Light)),
        Some(Bytes::from_static(b"\x1b[?997;2n"))
    );

    assert!(pane.apply_host_terminal_appearance(None).is_none());
    let unknown_query = pane.process_pty_bytes(pane_id, 0, b"\x1b[?996n", &tx);
    assert!(unknown_query.terminal_responses.is_empty());
    assert!(pane
        .apply_host_terminal_appearance(Some(crate::terminal_theme::HostAppearance::Dark))
        .is_none());

    pane.process_pty_bytes(pane_id, 0, b"\x1bc", &tx);
    assert!(pane
        .apply_host_terminal_appearance(Some(crate::terminal_theme::HostAppearance::Light))
        .is_none());
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_xtgettcap_truecolor_query_responses_without_queuing_input() {
    let (tx, mut rx) = mpsc::channel(8);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(
        pane_id,
        0,
        b"\x1bP+q5463;524742;73657472676266;73657472676262\x1b\\",
        &tx,
    );

    assert_eq!(
        result.terminal_responses,
        vec![
            expected_xtgettcap_response("5463", None),
            expected_xtgettcap_response("524742", Some(b"8")),
            expected_xtgettcap_response("73657472676266", Some(b"\\E[38:2:%p1%d:%p2%d:%p3%dm")),
            expected_xtgettcap_response("73657472676262", Some(b"\\E[48:2:%p1%d:%p2%d:%p3%dm")),
        ]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_split_xtgettcap_query_response() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1bP+q4", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"D73\x1b", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"\\", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![expected_xtgettcap_response(
            "4D73",
            Some(b"\\E]52;%p1%s;%p2%s\\007")
        )]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_orders_device_attribute_reply_before_following_xtgettcap_reply() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b[c\x1bP+q5463\x1b\\", &tx);

    assert_eq!(result.terminal_responses.len(), 2);
    assert!(String::from_utf8_lossy(&result.terminal_responses[0]).contains('c'));
    assert_eq!(
        result.terminal_responses[1],
        expected_xtgettcap_response("5463", None)
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_orders_xtgettcap_reply_before_following_device_attribute_reply() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1bP+q5463\x1b\\\x1b[c", &tx);

    assert_eq!(result.terminal_responses.len(), 2);
    assert_eq!(
        result.terminal_responses[0],
        expected_xtgettcap_response("5463", None)
    );
    assert!(String::from_utf8_lossy(&result.terminal_responses[1]).contains('c'));
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_orders_xtgettcap_reply_before_following_default_color_reply() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x00,
            g: 0x2b,
            b: 0x36,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1bP+q5463\x1b\\\x1b]11;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![
            expected_xtgettcap_response("5463", None),
            Bytes::from_static(b"\x1b]11;rgb:0000/2b2b/3636\x1b\\"),
        ]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn host_theme_update_preserves_child_default_color_override() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;#112233\x07", &tx);
    assert!(result.terminal_responses.is_empty());

    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;?\x07", &tx);
    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:1111/2222/3333\x07")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn child_default_color_reset_restores_cached_host_color() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"\x1b]11;#112233\x07", &tx);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        ..Default::default()
    });
    pane.process_pty_bytes(pane_id, 0, b"\x1b]111\x07", &tx);
    assert!(!pane.has_transient_default_color_override());

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;?\x07", &tx);
    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:aaaa/bbbb/cccc\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_recovers_xtgettcap_after_osc_bel_terminator() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]0;title\x07\x1bP+q5463\x1b\\", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![expected_xtgettcap_response("5463", None)]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_ignores_unknown_and_unsupported_xtgettcap_queries() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1bP+q6E6F7065;4D7\x1b\\", &tx);

    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_underline_color_xtgettcap_query_responses() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(
        pane_id,
        0,
        b"\x1bP+q5375;536D756C78;536574756C63\x1b\\",
        &tx,
    );

    assert_eq!(
        result.terminal_responses,
        vec![
            expected_xtgettcap_response("5375", None),
            expected_xtgettcap_response("536D756C78", Some(b"\\E[4:%p1%dm")),
            expected_xtgettcap_response(
                "536574756C63",
                Some(b"\\E[58:2::%p1%{65536}%/%d:%p1%{256}%/%{255}%&%d:%p1%{255}%&%d%;m")
            ),
        ]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn render_preserves_underline_color() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[4m\x1b[58:2::17:34:51mU");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let style = terminal.backend().buffer()[(0, 0)].style();
    assert!(style.add_modifier.contains(Modifier::UNDERLINED));
    assert_eq!(style.underline_color, Some(Color::Rgb(17, 34, 51)));
}

#[test]
fn dirty_patch_preserves_curly_underline_style() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[4:3mU");
    }

    let patch = match pane.collect_dirty_patch(20, 5) {
        TerminalDirtyPatchOutcome::Patch(patch) => patch,
        other => panic!("expected dirty patch, got {other:?}"),
    };

    let cell = &patch.rows[0].1[0];
    assert_eq!(cell.symbol, "U");
    assert_eq!(
        crate::protocol::underline_style_from_modifier(cell.modifier),
        3
    );
}

#[test]
fn full_frame_preserves_curly_underline_style() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b[4:3mU");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let frame = crate::protocol::FrameData::from_ratatui_buffer(terminal.backend().buffer(), None);
    assert_eq!(frame.cells[0].symbol, "U");
    assert_eq!(
        crate::protocol::underline_style_from_modifier(frame.cells[0].modifier),
        3
    );
}

#[test]
fn process_pty_bytes_orders_default_color_reply_before_following_device_attribute_reply() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x00,
            g: 0x2b,
            b: 0x36,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;?\x07\x1b[c", &tx);

    assert_eq!(result.terminal_responses.len(), 2);
    assert_eq!(
        result.terminal_responses[0],
        Bytes::from_static(b"\x1b]11;rgb:0000/2b2b/3636\x1b\\")
    );
    assert!(String::from_utf8_lossy(&result.terminal_responses[1]).contains('c'));
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_host_palette_color_without_queuing_input() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(
        crate::terminal_theme::TerminalTheme::default().with_palette_color(
            0,
            crate::terminal_theme::RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            },
        ),
    );

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;0;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]4;0;rgb:1111/2222/3333\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn opentui_256_palette_query_burst_uses_host_snapshot() {
    use std::fmt::Write as _;

    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    let mut theme = crate::terminal_theme::TerminalTheme::default();
    let mut queries = String::new();
    for index in 0..=u8::MAX {
        theme = theme.with_palette_color(
            index,
            crate::terminal_theme::RgbColor {
                r: index,
                g: 0x22,
                b: 0x33,
            },
        );
        let _ = write!(queries, "\x1b]4;{index};?\x07");
    }
    pane.apply_host_terminal_theme(theme);

    let result = pane.process_pty_bytes(pane_id, 0, queries.as_bytes(), &tx);

    assert_eq!(result.terminal_responses.len(), 256);
    assert_eq!(
        result.terminal_responses[0],
        Bytes::from_static(b"\x1b]4;0;rgb:0000/2222/3333\x1b\\")
    );
    assert_eq!(
        result.terminal_responses[255],
        Bytes::from_static(b"\x1b]4;255;rgb:ffff/2222/3333\x1b\\")
    );
}

#[test]
fn child_palette_override_survives_host_refresh_until_reset() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(
        crate::terminal_theme::TerminalTheme::default().with_palette_color(
            7,
            crate::terminal_theme::RgbColor {
                r: 0x11,
                g: 0x22,
                b: 0x33,
            },
        ),
    );
    pane.process_pty_bytes(pane_id, 0, b"\x1b]4;7;rgb:aa/bb/cc\x1b\\", &tx);

    pane.apply_host_terminal_theme(
        crate::terminal_theme::TerminalTheme::default().with_palette_color(
            7,
            crate::terminal_theme::RgbColor {
                r: 0x44,
                g: 0x55,
                b: 0x66,
            },
        ),
    );
    let overridden = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;7;?\x1b\\", &tx);
    assert_eq!(
        overridden.terminal_responses,
        vec![Bytes::from_static(b"\x1b]4;7;rgb:aaaa/bbbb/cccc\x1b\\")]
    );

    pane.process_pty_bytes(pane_id, 0, b"\x1b]104;7\x1b\\", &tx);
    let reset = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;7;?\x1b\\", &tx);
    assert_eq!(
        reset.terminal_responses,
        vec![Bytes::from_static(b"\x1b]4;7;rgb:4444/5555/6666\x1b\\")]
    );
}

#[test]
fn process_pty_bytes_returns_split_palette_color_query_response() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    let color = current_palette_color(&pane, 255);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;25", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"5;?\x1b", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"\\", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![expected_osc_rgb_response("4;255", color)]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_ignores_malformed_and_preserves_multi_palette_queries() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(
            pane_id,
            0,
            b"\x1b]4;;?\x07\x1b]4;-1;?\x07\x1b]4;256;?\x07\x1b]4;0;?;1;?\x07\x1b]4;0;rgb:1111/2222/3333\x07",
            &tx,
        );

    assert_eq!(result.terminal_responses.len(), 1);
    assert!(result.terminal_responses[0].starts_with(b"\x1b]4;0;rgb:"));
    assert_eq!(
        result.terminal_responses[0]
            .windows(4)
            .filter(|window| *window == b"rgb:")
            .count(),
        2
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_orders_palette_reply_before_following_terminal_replies() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    let color = current_palette_color(&pane, 0);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x00,
            g: 0x2b,
            b: 0x36,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;0;?\x07\x1b]11;?\x07\x1b[c", &tx);

    assert_eq!(result.terminal_responses.len(), 3);
    assert_eq!(
        result.terminal_responses[0],
        expected_osc_rgb_response("4;0", color)
    );
    assert_eq!(
        result.terminal_responses[1],
        Bytes::from_static(b"\x1b]11;rgb:0000/2b2b/3636\x1b\\")
    );
    assert!(String::from_utf8_lossy(&result.terminal_responses[2]).contains('c'));
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_default_color_query_responses_without_queuing_input() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x00,
            g: 0x2b,
            b: 0x36,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:0000/2b2b/3636\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_preserves_untracked_multi_color_query_responses() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0x65,
            g: 0x7b,
            b: 0x83,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xfd,
            g: 0xf6,
            b: 0xe3,
        }),
        ..Default::default()
    });

    let palette = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;0;?;1;?\x1b\\", &tx);
    let palette_response = palette.terminal_responses.concat();
    assert!(palette_response.starts_with(b"\x1b]4;0;rgb:"));
    assert_eq!(
        palette_response
            .windows(4)
            .filter(|window| *window == b"rgb:")
            .count(),
        2
    );

    let defaults = pane.process_pty_bytes(pane_id, 0, b"\x1b]10;?;?;?\x1b\\", &tx);
    let default_response = defaults.terminal_responses.concat();
    assert!(
        default_response.starts_with(b"\x1b]10;rgb:"),
        "unexpected default-color report: {:?}",
        String::from_utf8_lossy(&default_response)
    );
    assert_eq!(
        default_response
            .windows(4)
            .filter(|window| *window == b"rgb:")
            .count(),
        3
    );
    let core = pane.core.lock().unwrap();
    assert!(!core.child_default_foreground_changed);
    assert!(!core.child_default_background_changed);
    drop(core);
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_preserves_earlier_aggregate_palette_reply() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]4;0;?;1;?\x1b\\\x1b]4;0;?\x1b\\", &tx);

    assert_eq!(result.terminal_responses.len(), 2);
    assert_eq!(
        result.terminal_responses[0]
            .windows(4)
            .filter(|window| *window == b"rgb:")
            .count(),
        2
    );
    assert!(result.terminal_responses[1].starts_with(b"\x1b]4;0;rgb:"));
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_preserves_libghostty_reply_for_child_color_override() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"\x1b]10;rgb:11/22/33\x07", &tx);
    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]10;?\x1b\\", &tx);

    assert_eq!(result.terminal_responses.len(), 1);
    assert!(result.terminal_responses[0].starts_with(b"\x1b]10;rgb:1111/2222/3333"));
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_tracks_later_multi_value_color_set() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"\x1b]10;?;rgb:44/55/66\x1b\\", &tx);

    let core = pane.core.lock().unwrap();
    assert!(!core.child_default_foreground_changed);
    assert!(core.child_default_background_changed);
}

#[test]
fn process_pty_bytes_returns_cursor_color_query_response_from_foreground_fallback() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0x65,
            g: 0x7b,
            b: 0x83,
        }),
        background: None,
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]12;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]12;rgb:6565/7b7b/8383\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_cursor_color_query_response_from_child_foreground() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0x65,
            g: 0x7b,
            b: 0x83,
        }),
        background: None,
        ..Default::default()
    });

    pane.process_pty_bytes(pane_id, 0, b"\x1b]10;rgb:11/22/33\x07", &tx);
    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]12;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]12;rgb:1111/2222/3333\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_explicit_cursor_color_query_response() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0x65,
            g: 0x7b,
            b: 0x83,
        }),
        background: None,
        ..Default::default()
    });

    pane.process_pty_bytes(pane_id, 0, b"\x1b]12;rgb:11/22/33\x07", &tx);
    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]12;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]12;rgb:1111/2222/3333\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_default_color_query_responses_in_order() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0x65,
            g: 0x7b,
            b: 0x83,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xfd,
            g: 0xf6,
            b: 0xe3,
        }),
        ..Default::default()
    });

    let result =
        pane.process_pty_bytes(pane_id, 0, b"\x1b]10;?\x07\x1b]11;?\x07\x1b]12;?\x07", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![
            Bytes::from_static(b"\x1b]10;rgb:6565/7b7b/8383\x1b\\"),
            Bytes::from_static(b"\x1b]11;rgb:fdfd/f6f6/e3e3\x1b\\"),
            Bytes::from_static(b"\x1b]12;rgb:6565/7b7b/8383\x1b\\"),
        ]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_split_default_color_query_response() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xfd,
            g: 0xf6,
            b: 0xe3,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b";?\x1b", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"\\", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:fdfd/f6f6/e3e3\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_returns_split_cursor_color_query_response() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xfd,
            g: 0xf6,
            b: 0xe3,
        }),
        background: None,
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]12", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b";?\x1b", &tx);
    assert!(result.terminal_responses.is_empty());
    assert!(rx.try_recv().is_err());
    let result = pane.process_pty_bytes(pane_id, 0, b"\\", &tx);

    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]12;rgb:fdfd/f6f6/e3e3\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn process_pty_bytes_tracks_default_color_set_and_reset_before_replying() {
    let (tx, mut rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);
    pane.apply_host_terminal_theme(crate::terminal_theme::TerminalTheme {
        foreground: None,
        background: Some(crate::terminal_theme::RgbColor {
            r: 0xfd,
            g: 0xf6,
            b: 0xe3,
        }),
        ..Default::default()
    });

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]11;rgb:11/22/33\x07\x1b]11;?\x07", &tx);
    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:1111/2222/3333\x07")]
    );
    assert!(rx.try_recv().is_err());

    let result = pane.process_pty_bytes(pane_id, 0, b"\x1b]111\x07\x1b]11;?\x07", &tx);
    assert_eq!(
        result.terminal_responses,
        vec![Bytes::from_static(b"\x1b]11;rgb:fdfd/f6f6/e3e3\x1b\\")]
    );
    assert!(rx.try_recv().is_err());
}

#[test]
fn render_leaves_host_default_background_transparent() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let host_theme = crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33,
        }),
        ..Default::default()
    };
    pane.apply_host_terminal_theme(host_theme);
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"hi");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().fg, Some(Color::Reset));
    assert_eq!(buffer[(0, 0)].style().bg, Some(Color::Reset));
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().fg, Some(Color::Reset));
    assert_eq!(buffer[(2, 0)].style().bg, Some(Color::Reset));
}

#[test]
fn render_keeps_explicit_default_foreground_when_it_differs_from_host() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let host_theme = crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33,
        }),
        ..Default::default()
    };
    pane.apply_host_terminal_theme(host_theme);
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b]10;rgb:44/55/66\x1b\\hi");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let expected_fg = Some(Color::Rgb(0x44, 0x55, 0x66));
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().fg, expected_fg);
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().fg, expected_fg);
}

#[test]
fn render_keeps_explicit_default_background_when_it_differs_from_host() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let host_theme = crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33,
        }),
        ..Default::default()
    };
    pane.apply_host_terminal_theme(host_theme);
    {
        let mut core = pane.core.lock().unwrap();
        core.terminal.write(b"\x1b]11;rgb:44/55/66\x1b\\hi");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let expected_bg = Some(Color::Rgb(0x44, 0x55, 0x66));
    assert_eq!(buffer[(0, 0)].symbol(), "h");
    assert_eq!(buffer[(0, 0)].style().bg, expected_bg);
    assert_eq!(buffer[(2, 0)].symbol(), " ");
    assert_eq!(buffer[(2, 0)].style().bg, expected_bg);
}

#[test]
fn render_inverse_text_swaps_fg_and_resolved_bg_when_bg_is_transparent() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(20, 5, 0).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();
    let host_theme = crate::terminal_theme::TerminalTheme {
        foreground: Some(crate::terminal_theme::RgbColor {
            r: 0xaa,
            g: 0xbb,
            b: 0xcc,
        }),
        background: Some(crate::terminal_theme::RgbColor {
            r: 0x11,
            g: 0x22,
            b: 0x33,
        }),
        ..Default::default()
    };
    pane.apply_host_terminal_theme(host_theme);
    {
        let mut core = pane.core.lock().unwrap();
        // SGR 7 enables inverse/reverse video
        core.terminal.write(b"\x1b[7mhi\x1b[27m");
    }

    let backend = ratatui::backend::TestBackend::new(20, 5);
    let mut terminal = ratatui::Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| pane.render(frame, Rect::new(0, 0, 20, 5), false))
        .unwrap();

    let buffer = terminal.backend().buffer();
    let cell = &buffer[(0, 0)];
    assert_eq!(cell.symbol(), "h");
    // After inverse: fg should be the resolved bg, bg should be the original fg.
    // fg must NOT be Color::Reset (which would be the same hue as bg).
    assert_eq!(cell.style().fg, Some(Color::Rgb(0x11, 0x22, 0x33)));
    assert_eq!(cell.style().bg, Some(Color::Rgb(0xaa, 0xbb, 0xcc)));
}

#[test]
fn trim_trailing_blank_rows_drops_empty_viewport_tail() {
    let mut rows = vec!["hello".to_string(), "".to_string(), "   ".to_string()];
    trim_trailing_blank_rows(&mut rows);
    assert_eq!(rows, vec!["hello".to_string()]);
}
