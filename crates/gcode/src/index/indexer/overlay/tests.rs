use super::{
    DEFAULT_GIT_STATUS_TIMEOUT_SECS, git_status_timeout, route_candidates,
    valid_porcelain_status_byte,
};
use crate::index::walker::op_counts;
use std::time::Duration;

#[test]
fn porcelain_status_byte_validation_matches_git_v1_codes() {
    for byte in *b" MADRCU?!" {
        assert!(valid_porcelain_status_byte(byte));
    }
    for byte in [0, b'X', b'\n'] {
        assert!(!valid_porcelain_status_byte(byte));
    }
}

#[test]
#[serial_test::serial]
fn git_status_timeout_reads_positive_env_seconds() {
    temp_env::with_var("GCODE_GIT_STATUS_TIMEOUT_SECS", Some("2"), || {
        assert_eq!(git_status_timeout(), Duration::from_secs(2));
    });
}

#[test]
#[serial_test::serial]
fn git_status_timeout_rejects_invalid_env_values() {
    for value in ["0", "-1", "abc"] {
        temp_env::with_var("GCODE_GIT_STATUS_TIMEOUT_SECS", Some(value), || {
            assert_eq!(
                git_status_timeout(),
                Duration::from_secs(DEFAULT_GIT_STATUS_TIMEOUT_SECS)
            );
        });
    }
}

#[test]
fn overlay_gap_routing_shares_one_visibility_walk_and_context_load() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let root = tmp.path();
    let rels: Vec<String> = (0..24)
        .map(|index| format!("src/group_{}/module_{index}.rs", index % 4))
        .collect();
    for rel in &rels {
        let path = root.join(rel);
        std::fs::create_dir_all(path.parent().expect("parent dir")).expect("create gap dir");
        std::fs::write(&path, "pub fn gap() {}\n").expect("write gap file");
    }

    op_counts::reset();
    let (ast_by_rel, content_by_rel) = route_candidates(root, &rels, &[], true);
    let counts = op_counts::snapshot();

    assert_eq!(ast_by_rel.len(), rels.len(), "every gap routes to AST");
    assert!(content_by_rel.is_empty(), "no gap routes to content-only");
    assert_eq!(
        counts.visibility_walks, 1,
        "all gaps share one ancestor-pruned walk"
    );
    assert_eq!(
        counts.hidden_context_loads, 1,
        "all gaps share one hidden-path context"
    );
}
