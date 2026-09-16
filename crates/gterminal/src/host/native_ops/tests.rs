use super::{kill_group, snapshot_mode, truncate_snapshot, KillGroupError};
use crate::protocol::SnapshotMode;
use serde_json::{json, Map, Value};

#[test]
fn kill_group_refuses_non_positive_pgid() {
    assert!(matches!(
        kill_group(0, libc::SIGTERM),
        Err(KillGroupError::InvalidPgid)
    ));
    assert!(matches!(
        kill_group(-1, libc::SIGKILL),
        Err(KillGroupError::InvalidPgid)
    ));
}

#[test]
fn snapshot_mode_defaults_to_text_and_refuses_everything_else() {
    assert_eq!(snapshot_mode(&Map::new()).unwrap(), SnapshotMode::Text);
    for (wire, expected) in [("text", SnapshotMode::Text), ("ansi", SnapshotMode::Ansi)] {
        let mut extra = Map::new();
        extra.insert("mode".into(), Value::String(wire.into()));
        assert_eq!(snapshot_mode(&extra).unwrap(), expected);
    }
    for value in [
        json!("ANSI"),
        json!("plain"),
        json!(""),
        Value::Null,
        json!(1),
        json!(["ansi"]),
        json!({"mode": "ansi"}),
    ] {
        let mut extra = Map::new();
        extra.insert("mode".into(), value.clone());
        let refusal = snapshot_mode(&extra).expect_err("unsupported mode must be refused");
        assert_eq!(refusal["ok"], false, "{value}");
        assert_eq!(refusal["error"], "invalid_mode", "{value}");
        assert_eq!(refusal["valid_modes"], json!(["text", "ansi"]), "{value}");
    }
}

#[test]
fn truncate_snapshot_keeps_the_line_tail_and_counts_what_it_dropped() {
    let full = "one\ntwo\nthree\n";
    let whole = truncate_snapshot(full, 10, 1024);
    assert_eq!(whole.text, "one\ntwo\nthree");
    assert!(!whole.truncated);
    assert_eq!(whole.dropped_bytes, 0);
    assert_eq!(whole.total_bytes, full.len() as u64);

    let tail = truncate_snapshot(full, 2, 1024);
    assert_eq!(tail.text, "two\nthree");
    assert!(tail.truncated);
    assert_eq!(tail.dropped_bytes, "one\n".len() as u64);
    assert_eq!(tail.total_bytes, full.len() as u64);
}

#[test]
fn truncate_snapshot_trims_multibyte_text_on_char_boundaries() {
    let text = "\u{e9}\u{e9}\u{e9}";
    let exact = truncate_snapshot(text, 10, text.len());
    assert_eq!(exact.text, text);
    assert!(!exact.truncated);

    let split = truncate_snapshot(text, 10, 3);
    assert_eq!(split.text, "\u{e9}");
    assert!(split.text.len() <= 3);
    assert!(split.truncated);
    assert_eq!(split.dropped_bytes, 4);
    assert_eq!(split.total_bytes, text.len() as u64);

    let both = truncate_snapshot("aaa\n\u{e9}\u{e9}\u{e9}\n", 1, 3);
    assert_eq!(both.text, "\u{e9}");
    assert!(both.truncated);
    assert_eq!(both.dropped_bytes, 8);
    assert_eq!(both.total_bytes, 11);
}
