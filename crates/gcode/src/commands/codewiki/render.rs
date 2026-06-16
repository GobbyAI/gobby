mod common;
mod diagrams;
mod overview;
mod pages;
mod repo;

pub(crate) use common::model_degraded_sources;
#[cfg(test)]
pub(crate) use diagrams::bounded_component_edges;
pub(crate) use diagrams::{
    collect_subsystem_dependency_edges, render_architecture_structure_mermaid,
    render_module_call_mermaid, render_module_dependency_mermaid,
    render_subsystem_dependency_mermaid,
};
pub(crate) use overview::{render_architecture_doc, render_hotspots_doc, render_onboarding_doc};
pub(crate) use pages::{render_file_doc, render_module_doc};
pub(crate) use repo::build_repo_doc;
