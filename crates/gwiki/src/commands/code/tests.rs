use super::doc_paths::{write_doc, write_doc_before_persist};
use super::frontmatter::{source_files_from_frontmatter, unquote_yaml_string};
use super::io::{read_codewiki_meta, source_hashes_for_doc};
use super::*;

mod support;

mod ai;
mod architecture;
mod audit;
mod changes;
mod concepts;
mod concurrency;
mod contract;
mod features;
mod graph;
mod hotspots;
mod incremental;
mod infrastructure;
mod invalidation;
mod io_safety;
mod lock;
mod modules;
mod onboarding;
mod progress;
mod provenance;
mod publication;
mod purge;
mod repair;
mod reuse;
mod truth_digest;

/// Collect the generated doc set through the single options entry point,
/// mirroring the retired test wrappers (#17534): warn and return an empty set
/// on a hard generation error.
pub(crate) fn collect_docs(
    input: &CodewikiInput,
    options: GenerateDocsOptions<'_, '_>,
) -> Vec<BuiltDoc> {
    let mut docs = Vec::new();
    generate_hierarchical_docs(input, options, &mut |doc| {
        docs.push(doc);
        Ok(())
    })
    .unwrap_or_else(|error| {
        panic!("codewiki generation failed without ownership metadata: {error:#}")
    });
    docs
}

/// [`collect_docs`] projected to `(path, content)` pairs — the shape of the
/// retired tuple-returning public entry point.
pub(crate) fn collect_doc_pairs(
    input: &CodewikiInput,
    options: GenerateDocsOptions<'_, '_>,
) -> Vec<(String, String)> {
    collect_docs(input, options)
        .into_iter()
        .map(|doc| (doc.path, doc.content))
        .collect()
}

#[test]
fn documents_code_and_config_excludes_content_only_by_default() {
    // Code and structured config (json/yaml) are documented.
    assert!(should_document_file("crates/gcode/src/lib.rs", false));
    assert!(should_document_file(
        "src/gobby/data/docker-compose.services.yml",
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
    assert!(!should_document_file("Cargo.toml", false));
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

    let docs = collect_doc_pairs(
        &input,
        GenerateDocsOptions {
            generate: Some(&mut generator),
            ..Default::default()
        },
    );

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
        marker @ (b'`' | b'~') => marker,
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

#[test]
fn ai_generated_page_count_blocks_only_on_ai_routes() {
    use super::run::ai_generated_page_count;
    use super::types::{CodewikiDocMeta, CodewikiMeta};

    let doc = |route: &str| CodewikiDocMeta {
        ai_route: route.to_string(),
        ..CodewikiDocMeta::default()
    };

    // Fresh vault: nothing blocks a structural auto run.
    assert_eq!(ai_generated_page_count(&CodewikiMeta::default()), 0);

    // Structural and pre-AI entries never block; daemon/direct pages do.
    let mut meta = CodewikiMeta::default();
    meta.docs.insert("code/files/a.md".to_string(), doc("off"));
    meta.docs.insert("code/files/b.md".to_string(), doc(""));
    assert_eq!(ai_generated_page_count(&meta), 0);

    meta.docs
        .insert("code/files/c.md".to_string(), doc("daemon"));
    meta.docs
        .insert("code/files/d.md".to_string(), doc("direct"));
    assert_eq!(
        ai_generated_page_count(&meta),
        2,
        "a no-generator --ai auto run must refuse to rewrite these pages (#17776)"
    );
}
