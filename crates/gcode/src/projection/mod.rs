use crate::config::Context;
pub mod sync;

#[cfg(test)]
#[path = "tests/stale.rs"]
mod stale_tests;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectionReconcileFailure {
    pub target: sync::ProjectionTarget,
    pub message: String,
}

pub fn reconcile_deleted_file(_ctx: &Context, _file_path: &str) -> Vec<ProjectionReconcileFailure> {
    // A missing local selector does not make shared content stale. The retained
    // content-version GC performs exact symbol projection cleanup once no
    // machine references the version and it has aged out of git retention.
    Vec::new()
}
