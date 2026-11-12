use super::*;
use crate::pane::PaneRuntime;

#[test]
fn chunked_text_preserves_wraps_unicode_and_tail_limits() {
    for input in [
        "short\r\nsecond\r\ntail".to_owned(),
        "a\r\n\r\nb\r\n\r\n".repeat(40),
        format!("prefix\r\n{}tail", "\r\n".repeat(100)),
        "\x1b[31m盒🙂\x1b[0m long wrapped line\r\n".repeat(100),
        "x".repeat(4000),
    ] {
        let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
            13,
            4,
            crate::ghostty::ScrollbackLimits {
                bytes: 4_000_000,
                lines: 20_000,
            },
            &[],
            4,
        );
        pane.test_feed_output(input.as_bytes());
        let terminal = pane.terminal_handle();
        let full = terminal.ghostty.recent_unwrapped_text(usize::MAX);
        for max_lines in [0, 1, 2, 10, 100, 1000] {
            for max_bytes in [0, 1, 3, 12, 1024, 100_000] {
                let lines: Vec<_> = full.lines().collect();
                let limited = lines[lines.len().saturating_sub(max_lines)..].join("\n");
                let mut start = limited.len().saturating_sub(max_bytes);
                while !limited.is_char_boundary(start) {
                    start += 1;
                }
                let captured = terminal
                    .begin_history_capture()
                    .unwrap()
                    .read(SnapshotMode::Text, max_lines, max_bytes)
                    .unwrap();
                assert_eq!(
                    captured.text,
                    limited[start..],
                    "lines={max_lines} bytes={max_bytes} input={input:?}"
                );
            }
        }
    }
}

#[test]
fn overlapping_capture_is_refused_and_output_invalidates_chunks() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        20,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output("history\r\n".repeat(100).as_bytes());
    let terminal = pane.terminal_handle();
    let capture = terminal.begin_history_capture().unwrap();
    assert!(matches!(
        terminal.begin_history_capture(),
        Err("capture_busy")
    ));
    let mut changed = false;
    let result = capture.read_chunks(SnapshotMode::Text, 1000, 100_000, || {
        if !changed {
            pane.test_feed_output(b"new output");
            changed = true;
        }
    });
    assert!(matches!(result, Err("capture_invalidated")));
    drop(capture);
    assert!(terminal.begin_history_capture().is_ok());
}

#[test]
fn empty_history_capture_has_bounded_work() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        20,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output(" \r\n".repeat(10_000).as_bytes());
    let terminal = pane.terminal_handle();
    let mut chunks = 0;
    let snapshot = terminal
        .begin_history_capture()
        .unwrap()
        .read_chunks(SnapshotMode::Text, 10, 1024, || chunks += 1)
        .unwrap();
    assert!(chunks <= 2, "read {chunks} chunks for ten lines");
    assert!(snapshot.text.trim().is_empty());
    assert!(snapshot.truncated);
}

#[test]
fn soft_wrapped_blank_history_has_bounded_work() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        20,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output(b"old\r\n");
    pane.test_feed_output(" ".repeat(20 * 10_000).as_bytes());
    let mut chunks = 0;
    let result = pane
        .terminal_handle()
        .begin_history_capture()
        .unwrap()
        .read_chunks(SnapshotMode::Text, 1, 8, || chunks += 1);
    assert!(
        chunks <= 7,
        "read {chunks} chunks for an eight-byte capture"
    );
    assert!(matches!(result, Err("capture_limit")));
}

#[test]
fn held_capture_allows_input_and_same_pane_echo_before_release() {
    let (pane, mut rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        20,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output("history\r\n".repeat(200).as_bytes());
    let capture = pane.terminal_handle().begin_history_capture().unwrap();
    let (started_tx, started_rx) = std::sync::mpsc::channel();
    let (release_tx, release_rx) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        let mut first = true;
        capture.read_chunks(SnapshotMode::Text, 1000, 100_000, || {
            if first {
                first = false;
                started_tx.send(()).unwrap();
                release_rx
                    .recv_timeout(std::time::Duration::from_secs(5))
                    .unwrap();
            }
        })
    });
    started_rx
        .recv_timeout(std::time::Duration::from_secs(5))
        .unwrap();
    pane.input_writer()
        .try_send(bytes::Bytes::from_static(b"ECHO"))
        .unwrap();
    let received = rx.try_recv().unwrap();
    assert_eq!(received.as_ref(), b"ECHO");
    pane.test_feed_output(&received);
    let frame = pane.frame_data(20, 4);
    let buffer = frame.to_ratatui_buffer().unwrap();
    let visible: String = buffer.content.iter().map(|cell| cell.symbol()).collect();
    assert!(visible.contains("ECHO"));
    // Neither enqueue nor rendering depended on releasing the capture worker.
    release_tx.send(()).unwrap();
    assert!(matches!(worker.join().unwrap(), Err("capture_invalidated")));
}

#[test]
fn chunked_ansi_preserves_text_and_color_across_blank_rows() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        13,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output(
        "\x1b[31m盒🙂 wrapped content\r\n\r\n"
            .repeat(100)
            .as_bytes(),
    );
    let terminal = pane.terminal_handle();
    let snapshot = terminal
        .begin_history_capture()
        .unwrap()
        .read(SnapshotMode::Ansi, 1000, 1_000_000)
        .unwrap();
    assert!(!snapshot.truncated);
    let (replayed, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        13,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    replayed.test_feed_output(snapshot.text.replace('\n', "\r\n").as_bytes());
    assert_eq!(
        replayed
            .terminal_handle()
            .ghostty
            .recent_unwrapped_text(usize::MAX),
        terminal.ghostty.recent_unwrapped_text(usize::MAX)
    );
    let foreground = |frame: crate::protocol::FrameData| {
        frame
            .cells
            .into_iter()
            .find(|cell| cell.symbol == "盒")
            .unwrap()
            .fg
    };
    assert_eq!(
        foreground(replayed.frame_data(13, 4)),
        foreground(pane.frame_data(13, 4))
    );
}

#[test]
fn resize_invalidates_capture_between_chunks() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        20,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 4_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output("history\r\n".repeat(200).as_bytes());
    let terminal = pane.terminal_handle();
    let capture = terminal.begin_history_capture().unwrap();
    let result = capture.read_chunks(SnapshotMode::Text, 1000, 100_000, || {
        terminal.resize(4, 21, 0, 0);
    });
    assert!(matches!(result, Err("capture_invalidated")));
}

#[test]
fn chunked_selection_preserves_direction_columns_and_blank_rows() {
    for input in [
        "a\r\n\r\nb\r\n".repeat(100),
        format!("prefix\r\n{}tail", "\r\n".repeat(100)),
        "\x1b[31m盒🙂 wrapped content\r\n".repeat(100),
    ] {
        let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
            13,
            4,
            crate::ghostty::ScrollbackLimits {
                bytes: 4_000_000,
                lines: 20_000,
            },
            &[],
            4,
        );
        pane.test_feed_output(input.as_bytes());
        let terminal = pane.terminal_handle();
        for (a, x, b, y) in [
            (0, 0, 1000, 12),
            (1000, 2, 0, 5),
            (1000, 10, 0, 5),
            (60, 2, 30, 7),
            (3, 0, 1, 8),
            (0, 0, 0, 12),
        ] {
            let expected = terminal.ghostty.read_text_screen(a, x, b, y).unwrap();
            let actual = terminal
                .begin_history_capture()
                .unwrap()
                .read_selection(a, x, b, y)
                .unwrap();
            assert_eq!(actual, expected, "selection {a},{x} to {b},{y}");
        }
    }
}

#[test]
fn bounded_selection_keeps_utf8_lookahead_for_wire_truncation() {
    let (pane, _rx) = PaneRuntime::test_with_channel_and_scrollback_limits(
        80,
        4,
        crate::ghostty::ScrollbackLimits {
            bytes: 128_000_000,
            lines: 20_000,
        },
        &[],
        4,
    );
    pane.test_feed_output(
        format!("{}\r\n", "盒🙂abcdefgh".repeat(6))
            .repeat(12_000)
            .as_bytes(),
    );
    let terminal = pane.terminal_handle();
    let full = terminal.ghostty.read_text_screen(20_000, 0, 0, 79).unwrap();
    assert!(
        full.len() > crate::protocol::MAX_WRITE_BYTES,
        "fixture must exceed wire cap"
    );
    let text = terminal
        .begin_history_capture()
        .unwrap()
        .read_selection(20_000, 0, 0, 79)
        .unwrap();
    assert!(
        text.len() > crate::protocol::MAX_WRITE_BYTES,
        "wire encoder needs lookahead to report a truncated selection"
    );
    assert!(text.len() <= crate::protocol::MAX_WRITE_BYTES + 4);
    assert!(full.starts_with(&text));
}

#[test]
fn revision_resize_orders_the_pty_enqueue_under_the_terminal_mutation_lock() {
    let (pane, _rx) = PaneRuntime::test_with_channel(80, 24);
    let terminal = pane.terminal_handle();
    let revision = terminal.next_resize_revision();
    let mut queued = false;
    assert!(terminal.resize_for_revision(revision, 12, 40, |_| {
        assert!(
            terminal.history_mutation.try_lock().is_err(),
            "a newer emulator resize must wait until the PTY resize is queued"
        );
        queued = true;
    }));
    assert!(queued);
    let newer = terminal.next_resize_revision();
    assert!(!terminal.resize_for_revision(revision, 24, 80, |_| {
        panic!("superseded resize reached the PTY");
    }));
    assert!(terminal.resize_for_revision(newer, 16, 60, |_| {}));
}
