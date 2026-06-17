#[path = "build_parts/architecture.rs"]
mod architecture;
#[path = "build_parts/changes.rs"]
mod changes;
#[path = "build_parts/concepts.rs"]
mod concepts;
#[path = "build_parts/curated_content.rs"]
mod curated_content;
#[path = "build_parts/file.rs"]
mod file;
#[path = "build_parts/hotspots.rs"]
mod hotspots;
#[path = "build_parts/modules.rs"]
mod modules;
#[path = "build_parts/onboarding.rs"]
mod onboarding;
#[path = "build_parts/snapshot.rs"]
mod snapshot;

pub(crate) use architecture::build_architecture_doc;
pub(crate) use changes::build_codewiki_changes_doc;
pub(crate) use concepts::build_curated_navigation_docs;
pub(crate) use file::{FileDocPosition, build_file_doc};
pub(crate) use hotspots::build_hotspots_doc;
#[cfg(test)]
pub(crate) use modules::build_module_docs;
pub(crate) use modules::build_module_docs_with_filter;
pub(crate) use onboarding::build_onboarding_doc;
pub(crate) use snapshot::build_codewiki_index_snapshot;
