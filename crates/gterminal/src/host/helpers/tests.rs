use std::time::Instant;

use tokio::sync::mpsc;

use super::push_terminal_ansi;
use crate::host::state::Attachment;
use crate::protocol::render_ansi::BlitEncoder;
use crate::protocol::{CellData, FrameData, RenderEncoding, ServerMessage, TerminalFrame};

fn cell(symbol: &str, fg: u32) -> CellData {
    CellData {
        symbol: symbol.to_owned(),
        fg,
        bg: 0,
        modifier: 0,
        skip: false,
        hyperlink: None,
    }
}

fn frame(cells: Vec<CellData>) -> FrameData {
    FrameData {
        cells,
        width: 3,
        height: 2,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: crate::protocol::PaneModes::default(),
    }
}

fn attachment(capacity: usize) -> (Attachment, mpsc::Receiver<ServerMessage>) {
    let (tx, rx) = mpsc::channel(capacity);
    let att = Attachment {
        id: 1,
        host_terminal_id: "ht-1".to_owned(),
        encoding: RenderEncoding::TerminalAnsi,
        rows: 2,
        cols: 3,
        scroll: 0,
        reservation_id: None,
        tx,
        last_send: Instant::now(),
        desynced: true,
        delta_len: 0,
        delta_bytes: 0,
        encoder: BlitEncoder::new(),
    };
    (att, rx)
}

fn terminal_frame(rx: &mut mpsc::Receiver<ServerMessage>) -> TerminalFrame {
    match rx.try_recv().expect("a frame was queued") {
        ServerMessage::Terminal(frame) => frame,
        other => panic!("expected a terminal frame, got {other:?}"),
    }
}

#[test]
fn first_push_is_a_full_paint_with_real_cell_colors() {
    let (mut att, mut rx) = attachment(4);
    // Packed 24-bit colour, as `host::poll::apply_sgr` stores tmux SGR 38;2.
    let red = 0x02_00_00_00 | 0xff_00_00;
    let painted = frame(vec![cell("A", red); 6]);

    assert!(push_terminal_ansi(&mut att, &painted, 7));

    let sent = terminal_frame(&mut rx);
    assert!(sent.full);
    assert_eq!((sent.seq, sent.width, sent.height), (7, 3, 2));
    let text = String::from_utf8(sent.bytes).unwrap();
    assert_eq!(text.matches('A').count(), 6);
    assert!(
        text.contains("38;2;255;0;0"),
        "cell fg must be encoded: {text:?}"
    );
    assert!(!text.contains("38;2;255;255;255m\x1b[48;2;0;0;0m"));
    assert!(!att.desynced);
}

#[test]
fn unchanged_frame_sends_nothing_and_a_change_sends_a_delta() {
    let (mut att, mut rx) = attachment(4);
    let painted = frame(vec![cell("A", 0); 6]);
    assert!(push_terminal_ansi(&mut att, &painted, 1));
    let full_len = terminal_frame(&mut rx).bytes.len();

    assert!(!push_terminal_ansi(&mut att, &painted, 2));
    assert!(rx.try_recv().is_err(), "unchanged frame must not repaint");

    let mut changed = painted.clone();
    changed.cells[4] = cell("B", 0);
    assert!(push_terminal_ansi(&mut att, &changed, 3));
    let delta = terminal_frame(&mut rx);
    assert!(!delta.full);
    assert!(delta.bytes.len() < full_len);
    let text = String::from_utf8(delta.bytes).unwrap();
    assert_eq!(text.matches('B').count(), 1);
    assert_eq!(text.matches('A').count(), 0);
}

#[test]
fn dropped_delta_desyncs_and_the_next_push_repaints_in_full() {
    let (mut att, mut rx) = attachment(1);
    let painted = frame(vec![cell("A", 0); 6]);
    assert!(push_terminal_ansi(&mut att, &painted, 1));

    let mut changed = painted.clone();
    changed.cells[0] = cell("B", 0);
    assert!(!push_terminal_ansi(&mut att, &changed, 2), "queue is full");
    assert!(att.desynced);

    terminal_frame(&mut rx);
    assert!(push_terminal_ansi(&mut att, &changed, 3));
    let repaint = terminal_frame(&mut rx);
    assert!(repaint.full);
    let text = String::from_utf8(repaint.bytes).unwrap();
    assert_eq!(text.matches('A').count(), 5);
    assert_eq!(text.matches('B').count(), 1);
}
