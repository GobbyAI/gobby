use crate::config::Context;
use crate::graph::code_graph;
use crate::vector::code_symbols;

pub mod sync;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectionReconcileFailure {
    pub target: sync::ProjectionTarget,
    pub message: String,
}

pub fn reconcile_deleted_file(ctx: &Context, file_path: &str) -> Vec<ProjectionReconcileFailure> {
    let mut failures = Vec::new();

    if ctx.falkordb.is_some()
        && let Err(error) = code_graph::delete_file_projection(ctx, file_path)
    {
        failures.push(ProjectionReconcileFailure {
            target: sync::ProjectionTarget::Graph,
            message: error.to_string(),
        });
    }

    if let Some(qdrant) = ctx.qdrant.as_ref()
        && let Err(error) = code_symbols::delete_file_vectors(qdrant, &ctx.project_id, file_path)
    {
        failures.push(ProjectionReconcileFailure {
            target: sync::ProjectionTarget::Vectors,
            message: error.to_string(),
        });
    }

    failures
}
