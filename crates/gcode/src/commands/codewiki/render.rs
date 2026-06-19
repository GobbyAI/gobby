mod audit;
mod common;
mod diagrams;
mod features;
mod infrastructure;
mod overview;
mod pages;
mod repo;

pub(crate) use audit::{render_dead_code_doc, render_deprecations_doc};
pub(crate) use common::model_degraded_sources;
pub(crate) use diagrams::collect_subsystem_dependency_edges;
pub(crate) use features::render_feature_catalog_doc;
pub(crate) use infrastructure::render_infrastructure_doc;
pub(crate) use overview::{render_architecture_doc, render_hotspots_doc, render_onboarding_doc};
pub(crate) use pages::{render_file_doc, render_module_doc};
pub(crate) use repo::build_repo_doc;
