use super::{classify_input, InputEffect};

#[test]
fn focus_mouse_and_wheel_reports_do_not_edit_or_submit() {
    for input in [
        &b"\x1b[I"[..],
        b"\x1b[O",
        b"\x1b[<0;12;4M",
        b"\x1b[<0;12;4m",
        b"\x1b[<64;12;4M",
        b"\x1b[M \r!",
        b"\x1b[M\xff\xfe\xfd",
        b"\x1b[M\x1b[A",
        b"\x1b[I\x1b[<65;12;4M\x1b[O",
    ] {
        assert_eq!(
            classify_input(input),
            (InputEffect::Control, false),
            "{input:?}"
        );
    }
}

#[test]
fn cursor_reports_are_distinct_from_other_controls() {
    for input in [&b"\x1b[A"[..], b"\x1b[1;2D", b"\x1bOC", b"\x1b[I\x1b[B"] {
        assert_eq!(
            classify_input(input),
            (InputEffect::Cursor, false),
            "{input:?}"
        );
    }
}

#[test]
fn text_paste_and_unknown_edits_remain_human_input() {
    for input in [
        &b"hello"[..],
        "\u{1f642}".as_bytes(),
        b"\x1b[Ityped\x1b[O",
        b"text\x1b[M \r!",
        b"\x1b[200~hello\rworld\x1b[201~",
        b"\x1b",
        b"\x03",
        b"\x7f",
        b"\t",
        b"\x19",
        b"\x12",
        b"\x1b[3~",
        b"\x1b[", // Incomplete reports cannot establish an empty composer.
    ] {
        assert_eq!(
            classify_input(input),
            (InputEffect::Edit, false),
            "{input:?}"
        );
    }
}

#[test]
fn enter_submits_after_ignoring_reports() {
    for input in [&b"\r"[..], b"\x1bOM", b"hello\r", b"\x1b[I\r\x1b[O"] {
        assert_eq!(
            classify_input(input),
            (InputEffect::Edit, true),
            "{input:?}"
        );
    }
}
