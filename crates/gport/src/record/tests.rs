//! Unit tests for the record parser shared with `check-slice`.

use super::{found_work_lines, frozen_files, scan_kept};

/// Contract: the frozen list is the "- `path`" lines under the Frozen files heading, in order.
#[test]
fn frozen_files_reads_the_listed_paths_in_order() {
    let text = "# Port record\n\n## Frozen seam contract\n\n<!-- gport:kept seam-contract -->\n### Frozen files\n- `crates/a/src/one.rs`\n- `crates/b/src/two.rs`\n<!-- gport:end seam-contract -->\n";
    assert_eq!(
        frozen_files(text),
        ["crates/a/src/one.rs", "crates/b/src/two.rs"]
    );
}

/// Contract: the frozen list ends at the next heading, so later bullets are not frozen.
#[test]
fn frozen_files_stops_at_the_next_heading() {
    let text = "<!-- gport:kept seam-contract -->\n### Frozen files\n- `a.rs`\n## Later\n- `b.rs`\n<!-- gport:end seam-contract -->\n";
    assert_eq!(frozen_files(text), ["a.rs"]);
}

/// Contract: bullets above the Frozen files heading are not part of the frozen list.
#[test]
fn frozen_files_ignores_bullets_before_the_heading() {
    let text = "<!-- gport:kept seam-contract -->\n- `early.rs`\n### Frozen files\n- `a.rs`\n<!-- gport:end seam-contract -->\n";
    assert_eq!(frozen_files(text), ["a.rs"]);
}

/// Contract: a record without a seam-contract block, or with the heading outside it, freezes nothing.
#[test]
fn frozen_files_is_empty_without_the_seam_block() {
    assert!(frozen_files("# Port record\n").is_empty());
    let outside = "### Frozen files\n- `a.rs`\n<!-- gport:kept seam-contract -->\n<!-- gport:end seam-contract -->\n";
    assert!(frozen_files(outside).is_empty());
}

/// Contract: a line that is not exactly the "- `path`" form is not a frozen path.
#[test]
fn frozen_files_skips_lines_outside_the_list_form() {
    let text = "<!-- gport:kept seam-contract -->\n### Frozen files\n- `a.rs` with a note\n- b.rs\n- ``\n<!-- gport:end seam-contract -->\n";
    assert!(frozen_files(text).is_empty());
}

/// Contract: found:<id> returns the block's lines verbatim, blank lines included.
#[test]
fn found_work_lines_returns_the_block_body_verbatim() {
    let text = "### ALPHA\n<!-- gport:kept found:ALPHA -->\n- `crates/x.rs` fixed it\n\nnote line\n<!-- gport:end found:ALPHA -->\n";
    assert_eq!(
        found_work_lines(text, "ALPHA"),
        ["- `crates/x.rs` fixed it", "", "note line"]
    );
}

/// Contract: a chunk id matches its own block only, so R1 never reads R10.
#[test]
fn found_work_lines_match_the_whole_chunk_id() {
    let text = "<!-- gport:kept found:R10 -->\n- `r10.rs`\n<!-- gport:end found:R10 -->\n<!-- gport:kept found:R1 -->\n- `r1.rs`\n<!-- gport:end found:R1 -->\n";
    assert_eq!(found_work_lines(text, "R1"), ["- `r1.rs`"]);
    assert_eq!(found_work_lines(text, "R10"), ["- `r10.rs`"]);
}

/// Contract: a chunk with no found block has no Found Work lines.
#[test]
fn found_work_lines_is_empty_without_the_block() {
    assert!(found_work_lines("# Port record\n", "ALPHA").is_empty());
}

/// Contract: a marker counts only as a whole line, so an indented or trailing marker opens no block.
#[test]
fn markers_must_be_the_whole_line() {
    let text = "  <!-- gport:kept found:A -->\n- `x.rs`\n<!-- gport:kept found:A --> trailing\n";
    let scan = scan_kept(text);
    assert!(scan.blocks.is_empty());
    assert!(scan.problems.is_empty());
}

/// Contract: a block left open at end of file is reported as a problem, and its body is kept for the scan.
#[test]
fn unclosed_block_is_reported() {
    let scan = scan_kept("<!-- gport:kept found:A -->\n- `x.rs`\n");
    assert_eq!(scan.blocks.len(), 1);
    assert_eq!(scan.blocks[0].body, ["- `x.rs`"]);
    assert_eq!(scan.problems, ["kept block found:A is never closed"]);
}

/// Contract: an end marker with no open block is reported as a problem.
#[test]
fn stray_end_marker_is_reported() {
    let scan = scan_kept("<!-- gport:end found:A -->\n");
    assert!(scan.blocks.is_empty());
    assert_eq!(
        scan.problems,
        ["end marker of kept block found:A has no start marker"]
    );
}
