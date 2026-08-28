use tokio::sync::mpsc;

use super::*;
use crate::layout::PaneId;
use crate::pane::terminal::GhosttyPaneTerminal;

fn rendered_line(
    text: impl Into<String>,
    soft_wrapped: bool,
    wrap_continuation: bool,
) -> RenderedLine {
    RenderedLine {
        text: text.into(),
        soft_wrapped,
        wrap_continuation,
    }
}

#[test]
fn merges_overlapping_snapshots() {
    let mut cache = Vec::new();

    merge_snapshot(
        &mut cache,
        &[
            rendered_line("one", false, false),
            rendered_line("two", false, false),
        ],
    );
    merge_snapshot(
        &mut cache,
        &[
            rendered_line("two", false, false),
            rendered_line("three", false, false),
            rendered_line("four", false, false),
        ],
    );
    merge_snapshot(
        &mut cache,
        &[
            rendered_line("three", false, false),
            rendered_line("four", false, false),
            rendered_line("five", false, false),
        ],
    );

    let text: Vec<&str> = cache.iter().map(|line| line.text.as_str()).collect();
    assert_eq!(text, vec!["one", "two", "three", "four", "five"]);
}

#[test]
fn unwraps_soft_wrapped_rows() {
    let snapshot = vec![
        rendered_line("ABCDE", true, false),
        rendered_line("FGHIJ", false, true),
        rendered_line("next", false, false),
    ];

    assert_eq!(unwrap_render_lines(&snapshot), vec!["ABCDEFGHIJ", "next"]);
}

#[test]
fn suppresses_leading_wrap_continuation() {
    let snapshot = vec![
        rendered_line("suffix", false, true),
        rendered_line("next", false, false),
    ];

    assert_eq!(unwrap_render_lines(&snapshot), vec!["next"]);
}

#[test]
fn clears_on_blank_snapshot() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(40, 3, 1024).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    pane.process_pty_bytes(pane_id, 0, b"old\r\n", &tx);
    assert!(pane.recent_text(3).contains("old"));

    pane.process_pty_bytes(pane_id, 0, b"\x1b[2J\x1b[H", &tx);
    assert_eq!(pane.recent_text(3).trim(), "");
}

#[test]
fn ignores_alternate_screen_snapshots() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(40, 3, 1024).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    for line in 0..20 {
        pane.process_pty_bytes(pane_id, 0, format!("{line:06}\r\n").as_bytes(), &tx);
    }
    {
        let core = pane.core.lock().unwrap();
        assert!(core
            .recent_fallback
            .rows
            .iter()
            .any(|line| line.text.contains("000000")));
    }

    pane.process_pty_bytes(pane_id, 0, b"\x1b[?1049h\x1b[2J\x1b[H", &tx);

    let core = pane.core.lock().unwrap();
    assert!(core
        .recent_fallback
        .rows
        .iter()
        .any(|line| line.text.contains("000000")));
}

#[test]
fn invalidates_fallback_when_output_arrives_while_scrolled_up() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(40, 3, 1024).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx.clone()).unwrap();
    let pane_id = PaneId::from_raw(1);

    for line in 0..20 {
        pane.process_pty_bytes(pane_id, 0, format!("{line:06}\r\n").as_bytes(), &tx);
    }
    let before = pane.scroll_metrics().expect("scroll metrics before scroll");
    pane.set_scroll_offset_from_bottom(before.max_offset_from_bottom);
    pane.process_pty_bytes(pane_id, 0, b"new output\r\n", &tx);

    let core = pane.core.lock().unwrap();
    assert_eq!(recent_text(&core, 3, false).text, "");
    assert_eq!(recent_text(&core, 3, true).text, "");
}

#[test]
fn seed_history_updates_fallback() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(40, 3, 1024).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    pane.seed_history_ansi("seeded history\r\n");

    let core = pane.core.lock().unwrap();
    assert!(recent_text(&core, 3, false).text.contains("seeded history"));
    assert!(recent_text(&core, 3, true).text.contains("seeded history"));
}

#[test]
fn recent_ansi_unwrapped_limits_rendered_rows_before_unwrapping() {
    let (tx, _rx) = mpsc::channel(4);
    let terminal = crate::ghostty::Terminal::new(40, 3, 1024).unwrap();
    let pane = GhosttyPaneTerminal::new(terminal, tx).unwrap();

    {
        let mut core = pane.core.lock().unwrap();
        core.recent_fallback.rows = vec![
            rendered_line("older", false, false),
            rendered_line("wrapped", true, false),
            rendered_line("rows", false, true),
            rendered_line("last", false, false),
        ];
        core.recent_fallback.usable = true;
    }

    let snapshot = pane.recent_unwrapped_ansi_snapshot(3);
    assert_eq!(snapshot.text, "wrappedrows\nlast\n");
    assert!(snapshot.truncated);
    assert_eq!(pane.recent_ansi(3), "wrapped\nrows\nlast\n");
}
