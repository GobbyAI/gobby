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

#[test]
fn generated_codewiki_docs_have_no_md012_outside_fences() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![support::test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client;",
        )],
    };
    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::CURATED_NAVIGATION_SYSTEM {
            Some(support::test_curated_navigation_json())
        } else {
            Some("Generated prose.\n\n\nWith extra space.".to_string())
        }
    };

    let docs = generate_hierarchical_docs(&input, Some(&mut generator));

    assert!(!docs.is_empty());
    for (path, content) in docs {
        assert!(
            !has_multiple_blank_lines_outside_fences(&content),
            "{path} contains multiple blank lines outside fenced code"
        );
    }
}

fn has_multiple_blank_lines_outside_fences(markdown: &str) -> bool {
    let mut fence: Option<(u8, usize)> = None;
    let mut blank_run = 0_usize;
    for line in markdown.split('\n') {
        if let Some((marker, len)) = fence {
            if test_fence_closes(line, marker, len) {
                fence = None;
            }
            continue;
        }
        if let Some(opening) = test_fence_start(line.trim_end()) {
            fence = Some(opening);
            blank_run = 0;
            continue;
        }
        if line.trim().is_empty() {
            blank_run += 1;
            if blank_run > 1 {
                return true;
            }
        } else {
            blank_run = 0;
        }
    }
    false
}

fn test_fence_start(line: &str) -> Option<(u8, usize)> {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return None;
    }
    let trimmed = &line[leading_spaces..];
    let marker = match trimmed.as_bytes().first().copied()? {
        b'`' | b'~' => trimmed.as_bytes()[0],
        _ => return None,
    };
    let len = trimmed.bytes().take_while(|byte| *byte == marker).count();
    (len >= 3).then_some((marker, len))
}

fn test_fence_closes(line: &str, marker: u8, fence_len: usize) -> bool {
    let leading_spaces = line.len() - line.trim_start_matches(' ').len();
    if leading_spaces > 3 {
        return false;
    }
    let trimmed = &line[leading_spaces..];
    let len = trimmed.bytes().take_while(|byte| *byte == marker).count();
    len >= fence_len && trimmed[len..].trim().is_empty()
}
