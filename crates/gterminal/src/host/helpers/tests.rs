use std::time::Instant;

use super::push_terminal_ansi;
use crate::host::backpressure::FrameMailbox;
use crate::host::state::Attachment;
use crate::protocol::render_ansi::BlitEncoder;
use crate::protocol::{CellData, FrameData, RenderEncoding, ServerMessage, TerminalFrame};

fn cell(symbol: &str, fg: u32) -> CellData {
    styled_cell(symbol, fg, 0, 0)
}

fn styled_cell(symbol: &str, fg: u32, bg: u32, modifier: u16) -> CellData {
    CellData {
        symbol: symbol.to_owned(),
        fg,
        bg,
        modifier,
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

fn attachment() -> (Attachment, FrameMailbox) {
    let mailbox = FrameMailbox::new();
    let att = Attachment {
        id: 1,
        host_terminal_id: "ht-1".to_owned(),
        encoding: RenderEncoding::TerminalAnsi,
        rows: 2,
        cols: 3,
        scroll: 0,
        reservation_id: None,
        mailbox: mailbox.clone(),
        last_send: Instant::now(),
        desynced: true,
        delta_len: 0,
        delta_bytes: 0,
        encoder: BlitEncoder::new(),
    };
    (att, mailbox)
}

fn terminal_frame(mailbox: &FrameMailbox) -> TerminalFrame {
    match mailbox.try_pop().expect("a frame was queued") {
        ServerMessage::Terminal(frame) => frame,
        other => panic!("expected a terminal frame, got {other:?}"),
    }
}

#[test]
fn first_push_is_a_full_paint_with_real_cell_colors() {
    let (mut att, mailbox) = attachment();
    // Packed 24-bit colours, as `host::poll::apply_sgr` stores tmux SGR 38;2 / 48;2.
    let red = 0x02_00_00_00 | 0xff_00_00;
    let navy = 0x02_00_00_00 | 0x00_40_80;
    let bold = 1;
    let painted = frame(vec![styled_cell("A", red, navy, bold); 6]);

    assert!(push_terminal_ansi(&mut att, &painted, 7, usize::MAX));

    let sent = terminal_frame(&mailbox);
    assert!(sent.full);
    assert_eq!((sent.seq, sent.width, sent.height), (7, 3, 2));
    let text = String::from_utf8(sent.bytes).unwrap();
    assert_eq!(text.matches('A').count(), 6);
    assert!(
        text.contains("\x1b[0;1;38;2;255;0;0;48;2;0;64;128m"),
        "cell modifier, fg and bg must be encoded: {text:?}"
    );
    assert!(!text.contains("38;2;255;255;255m\x1b[48;2;0;0;0m"));
    assert!(!text.contains("38;2;0;0;0m\x1b[48;2;255;255;255m"));
    assert!(!att.desynced);
}

#[test]
fn unchanged_frame_sends_nothing_and_a_change_sends_a_delta() {
    let (mut att, mailbox) = attachment();
    let painted = frame(vec![cell("A", 0); 6]);
    assert!(push_terminal_ansi(&mut att, &painted, 1, usize::MAX));
    let full_len = terminal_frame(&mailbox).bytes.len();

    assert!(!push_terminal_ansi(&mut att, &painted, 2, usize::MAX));
    assert!(
        mailbox.try_pop().is_none(),
        "unchanged frame must not repaint"
    );

    let mut changed = painted.clone();
    changed.cells[4] = cell("B", 0);
    assert!(push_terminal_ansi(&mut att, &changed, 3, usize::MAX));
    let delta = terminal_frame(&mailbox);
    assert!(!delta.full);
    assert!(delta.bytes.len() < full_len);
    let text = String::from_utf8(delta.bytes).unwrap();
    assert_eq!(text.matches('B').count(), 1);
    assert_eq!(text.matches('A').count(), 0);
}

#[test]
fn overflow_replaces_queued_deltas_with_one_keyframe() {
    let (mut att, mailbox) = attachment();
    let painted = frame(vec![cell("A", 0); 6]);
    assert!(push_terminal_ansi(&mut att, &painted, 1, usize::MAX));
    let cap = mailbox.queued_bytes();

    let mut changed = painted.clone();
    changed.cells[0] = cell("B", 0);
    assert!(
        push_terminal_ansi(&mut att, &changed, 2, cap),
        "overflow still queues a replacement keyframe"
    );
    assert!(att.desynced);

    let repaint = terminal_frame(&mailbox);
    assert!(repaint.full);
    assert!(mailbox.try_pop().is_none(), "queue holds one keyframe");
    let text = String::from_utf8(repaint.bytes).unwrap();
    assert_eq!(text.matches('A').count(), 5);
    assert_eq!(text.matches('B').count(), 1);
}
