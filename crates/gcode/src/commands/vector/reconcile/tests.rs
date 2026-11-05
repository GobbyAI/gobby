use super::*;

#[cfg(gcode_postgres_tests)]
#[path = "tests/serial_db.rs"]
mod serial_db;

#[test]
fn inventory_digest_is_order_independent_and_content_sensitive() {
    let orphans = vec!["orphan-b".to_string(), "orphan-a".to_string()];
    let synced = vec!["version-2".to_string(), "version-1".to_string()];
    let digest = inventory_digest(&orphans, &synced);

    assert!(digest.starts_with("sha256:"), "digest prefix: {digest}");
    assert_eq!(
        digest.len(),
        "sha256:".len() + 64,
        "digest is a SHA-256 hex"
    );
    assert_eq!(
        digest,
        inventory_digest(
            &["orphan-a".to_string(), "orphan-b".to_string()],
            &["version-1".to_string(), "version-2".to_string()],
        ),
        "digest must not depend on input order"
    );
    assert_ne!(
        digest,
        inventory_digest(&orphans[..1], &synced),
        "removing an orphan must change the digest"
    );
    assert_ne!(
        digest,
        inventory_digest(&orphans, &synced[..1]),
        "removing a synced-missing version must change the digest"
    );
    assert_ne!(
        inventory_digest(&["shared-id".to_string()], &[]),
        inventory_digest(&[], &["shared-id".to_string()]),
        "an ID in the orphan group must not hash like the same ID in the version group"
    );
}
