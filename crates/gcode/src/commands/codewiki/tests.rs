use super::io::{
    read_codewiki_meta, source_files_from_frontmatter, source_hashes_for_doc, unquote_yaml_string,
    write_doc,
};
use super::*;

mod support;

mod ai;
mod architecture;
mod audit;
mod changes;
mod concepts;
mod contract;
mod features;
mod graph;
mod hotspots;
mod incremental;
mod infrastructure;
mod invalidation;
mod io_safety;
mod modules;
mod onboarding;
mod progress;
mod provenance;
mod repair;
mod reuse;
mod truth_digest;

#[test]
fn documents_code_and_config_excludes_content_only_by_default() {
    // Code and structured config (json/yaml) are documented.
    assert!(should_document_file("crates/gcode/src/lib.rs", false));
    assert!(should_document_file(
        "crates/gcore/assets/docker-compose.services.yml",
        false
    ));
    assert!(should_document_file(
        "crates/gcode/contract/gcode.contract.json",
        false
    ));

    // Content-only files (markdown, plain text, license) are gwiki's domain
    // and are skipped by default.
    assert!(!should_document_file("README.md", false));
    assert!(!should_document_file("docs/guides/codewiki.md", false));
    assert!(!should_document_file("LICENSE", false));

    // --include-docs opts content-only files back in.
    assert!(should_document_file("README.md", true));
    assert!(should_document_file("docs/guides/codewiki.md", true));
}
