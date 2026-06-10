use super::io::{
    read_codewiki_meta, source_files_from_frontmatter, source_hashes_for_doc, unquote_yaml_string,
    write_doc,
};
use super::*;

mod support;

mod ai;
mod architecture;
mod changes;
mod contract;
mod graph;
mod hotspots;
mod incremental;
mod io_safety;
mod modules;
mod onboarding;
mod progress;
mod provenance;
