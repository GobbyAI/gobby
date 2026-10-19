//! Warnings for skipped detection manifest rows (#24381, plan 2.12.4). Python's `_install_rows`
//! logs `Skipping invalid detection manifest` for each row it drops; the Rust set reports the same.

use gobby_daemon::pane_detection::ManifestSet;

const CLAUDE: &str = include_str!("../../../src/gobby/install/shared/detection/claude.toml");

#[test]
fn from_rows_logged_warns_once_per_skipped_row() {
    let rows = vec![
        ("claude".to_owned(), CLAUDE.to_owned()),
        ("droid".to_owned(), CLAUDE.to_owned()),
        ("agy".to_owned(), "id = [".to_owned()),
    ];
    let mut warnings = Vec::new();
    let set = ManifestSet::from_rows_logged(&rows, |line| warnings.push(line.to_owned()));

    assert!(set.for_provider("claude").is_some());
    assert!(set.for_provider("droid").is_none());
    assert!(set.for_provider("agy").is_none());
    assert_eq!(warnings.len(), 2, "warnings: {warnings:?}");
    assert!(
        warnings[0].starts_with("Skipping invalid detection manifest provider_id=droid error="),
        "warnings: {warnings:?}"
    );
    assert!(
        warnings[0].contains("disagrees with manifest id"),
        "warnings: {warnings:?}"
    );
    assert!(
        warnings[1].starts_with("Skipping invalid detection manifest provider_id=agy error="),
        "warnings: {warnings:?}"
    );
}
