mod audit;
mod common;
mod diagrams;
mod features;
mod infrastructure;
mod overview;
mod pages;
mod repo;

pub(crate) use audit::render_deprecations_doc;
pub(crate) use common::cell_summary;
pub(crate) use diagrams::{
    collect_subsystem_dependency_edges, module_diagram_context,
    render_module_call_sequence_with_context, render_module_dependency_mermaid_with_context,
};
#[cfg(test)]
pub(crate) use diagrams::{render_module_call_sequence, render_module_dependency_mermaid};
pub(crate) use features::render_feature_catalog_doc;
pub(crate) use infrastructure::render_infrastructure_doc;
pub(crate) use overview::{render_architecture_doc, render_hotspots_doc, render_onboarding_doc};
pub(crate) use pages::{render_file_doc, render_module_doc};
pub(crate) use repo::build_repo_doc;
