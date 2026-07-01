use super::super::lifecycle::cleanup_deleted_file_projections;
use super::super::pipeline::cleanup_skipped_file_if_indexed;
use super::super::{IndexDegradation, IndexOutcome};
use super::fixtures::test_context;
use crate::projection::sync::ProjectionTarget;

#[test]
fn explicit_skip_cleanup_deletes_stale_facts_and_projections() {
    let ctx = test_context();
    let mut outcome = IndexOutcome::new("project-1");
    let mut deleted = false;

    cleanup_skipped_file_if_indexed(
        &ctx,
        "src/setup.mjs",
        &mut outcome,
        true,
        Some(true),
        || {
            deleted = true;
            Ok(())
        },
    )
    .expect("cleanup skipped explicit file");

    assert!(deleted);
    assert_eq!(outcome.skipped_files, 1);
    assert_eq!(outcome.degraded.len(), 2);
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Graph,
            ..
        } if file_path == "src/setup.mjs"
    )));
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Vectors,
            ..
        } if file_path == "src/setup.mjs"
    )));
}

#[test]
fn explicit_skip_cleanup_ignores_never_indexed_files() {
    let ctx = test_context();
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_skipped_file_if_indexed(
        &ctx,
        "src/secret.txt",
        &mut outcome,
        false,
        Some(false),
        || panic!("delete should not run for files without stale facts"),
    )
    .expect("cleanup skipped explicit file");

    assert_eq!(outcome.skipped_files, 1);
    assert!(outcome.degraded.is_empty());
}

#[test]
fn deleted_file_projection_cleanup_degrades_without_services() {
    let ctx = test_context();
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_deleted_file_projections(&ctx, "src/deleted.rs", &mut outcome, Some(true));

    assert_eq!(outcome.degraded.len(), 2);
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Graph,
            message,
        } if file_path == "src/deleted.rs"
            && message.contains("FalkorDB is not configured")
    )));
    assert!(outcome.degraded.iter().any(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            file_path,
            target: ProjectionTarget::Vectors,
            message,
        } if file_path == "src/deleted.rs"
            && message.contains("Qdrant config is required")
    )));
}

#[test]
fn deleted_file_projection_cleanup_skips_vectors_when_not_previously_synced() {
    let ctx = test_context();
    let mut outcome = IndexOutcome::new("project-1");

    cleanup_deleted_file_projections(&ctx, "src/deleted.rs", &mut outcome, Some(false));

    assert_eq!(outcome.degraded.len(), 1);
    assert!(outcome.degraded.iter().all(|degradation| matches!(
        degradation,
        IndexDegradation::ProjectionCleanupFailed {
            target: ProjectionTarget::Graph,
            ..
        }
    )));
}
