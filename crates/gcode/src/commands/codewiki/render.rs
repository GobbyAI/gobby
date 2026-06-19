mod common;
mod diagrams;
mod overview;
mod pages;
mod repo;

pub(crate) use common::model_degraded_sources;
pub(crate) use diagrams::collect_subsystem_dependency_edges;
pub(crate) use overview::{render_architecture_doc, render_hotspots_doc, render_onboarding_doc};
pub(crate) use pages::{render_file_doc, render_module_doc};
pub(crate) use repo::build_repo_doc;
