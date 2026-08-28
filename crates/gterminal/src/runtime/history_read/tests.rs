use super::*;
use crate::ghostty::{ScreenTextCell, ScreenTextRow};

fn row(text: &str) -> ScreenTextRow {
    ScreenTextRow {
        cells: text
            .chars()
            .map(|ch| ScreenTextCell {
                wide: CellWide::Narrow,
                graphemes: vec![ch as u32],
            })
            .collect(),
        soft_wrapped: false,
        wrap_continuation: false,
    }
}

fn snapshot(lines: &[&str]) -> ScreenSnapshot {
    ScreenSnapshot {
        cols: 20,
        rows: lines.iter().map(|line| row(line)).collect(),
    }
}

#[test]
fn viewport_similarity_tolerates_small_dynamic_regions() {
    let initial = snapshot(&["line 1", "line 2", "worked for 2s", "prompt"]);
    let status_changed = snapshot(&["line 1", "line 2", "worked for 3s", "prompt"]);
    let scrolled = snapshot(&["older", "line 1", "line 2", "prompt"]);

    assert!(initial.similar_text(&status_changed));
    assert!(!initial.similar_text(&scrolled));
}

#[test]
fn controlled_upward_scroll_prepends_only_new_rows() {
    let previous = snapshot(&["line 3", "line 4", "line 5", "status"]);
    let next = snapshot(&["line 1", "line 2", "line 3", "line 4"]);
    let mut history = previous.rows.clone();

    assert_eq!(
        merge_scrolled_up(&mut history, &previous, &next),
        UpwardMerge::Advanced { rows: 2 }
    );
    assert_eq!(
        row_identities(&history),
        ["line 1", "line 2", "line 3", "line 4", "line 5", "status"]
    );
}

#[test]
fn fixed_header_is_not_repeated_or_counted_as_scrolled_history() {
    let previous = snapshot(&["sticky", "line 4", "line 5", "line 6", "line 7"]);
    let next = snapshot(&["sticky", "line 2", "line 3", "line 4", "line 5"]);
    let mut history = previous.rows.clone();

    assert_eq!(
        merge_scrolled_up(&mut history, &previous, &next),
        UpwardMerge::Advanced { rows: 2 }
    );
    assert_eq!(
        row_identities(&history),
        ["line 2", "line 3", "sticky", "line 4", "line 5", "line 6", "line 7"]
    );
}

#[test]
fn unchanged_and_unaligned_frames_do_not_change_history() {
    let previous = snapshot(&["line 1", "line 2", "line 3"]);
    let mut history = previous.rows.clone();

    assert_eq!(
        merge_scrolled_up(&mut history, &previous, &previous),
        UpwardMerge::Unchanged
    );
    assert_eq!(
        merge_scrolled_up(
            &mut history,
            &previous,
            &snapshot(&["other a", "other b", "other c"]),
        ),
        UpwardMerge::Unaligned
    );
    assert_eq!(history, previous.rows);
}

#[test]
fn snapshot_text_limits_rendered_rows_before_unwrapping() {
    let mut first = row("hello ");
    first.soft_wrapped = true;
    let mut second = row("world");
    second.wrap_continuation = true;
    let rows = vec![row("older"), first, second];

    assert_eq!(
        snapshot_text(&rows, 2, false, true),
        TerminalReadSnapshot {
            text: "hello\nworld\n".into(),
            truncated: true,
        }
    );
    assert_eq!(
        snapshot_text(&rows, 2, true, true),
        TerminalReadSnapshot {
            text: "helloworld\n".into(),
            truncated: true,
        }
    );
}
