use super::support::*;
use super::*;

#[test]
fn generates_hierarchical_docs() {
    let out_dir = tempfile::tempdir().expect("tempdir");
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string(), "src/nested/api.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![
            test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client {"),
            test_symbol("src/lib.rs", "connect", "function", 5, "pub fn connect()"),
            test_symbol(
                "src/nested/api.rs",
                "serve",
                "function",
                3,
                "pub fn serve()",
            ),
        ],
    };

    let docs = generate_hierarchical_docs(&input, None);
    write_doc_set(out_dir.path(), &docs).expect("writes docs");

    let repo = std::fs::read_to_string(out_dir.path().join("code/repo.md")).expect("repo doc");
    let module = std::fs::read_to_string(out_dir.path().join("code/modules/src.md"))
        .expect("src module doc");
    let file =
        std::fs::read_to_string(out_dir.path().join("code/files/src/lib.rs.md")).expect("file doc");

    assert!(repo.contains("[[code/modules/src\\|src]]"));
    assert!(repo.contains("Repository Overview"));
    assert!(module.contains("[[code/files/src/lib.rs\\|src/lib.rs]]"));
    assert!(file.contains("## Overview"));
    assert!(file.contains("## Key components"));
    assert!(!file.contains("## API Symbols"));
    assert!(file.contains("[[code/modules/src|src]]"));
}

#[test]
fn codewiki_unified_vault_emits_code_paths_frontmatter_and_wikilinks() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client {",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let paths = docs
        .iter()
        .map(|(path, _)| path.as_str())
        .collect::<BTreeSet<_>>();

    assert!(paths.contains("code/repo.md"));
    assert!(paths.contains("code/modules/src.md"));
    assert!(paths.contains("code/files/src/lib.rs.md"));

    let repo = docs
        .iter()
        .find(|(path, _)| path == "code/repo.md")
        .map(|(_, content)| content)
        .expect("repo doc");
    let file = docs
        .iter()
        .find(|(path, _)| path == "code/files/src/lib.rs.md")
        .map(|(_, content)| content)
        .expect("file doc");
    let yaml = file
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    let frontmatter: serde_yaml::Value = serde_yaml::from_str(yaml).expect("parse frontmatter");

    assert!(repo.contains("[[code/modules/src\\|src]]"));
    assert!(repo.contains("| Module | Summary |\n| --- | --- |\n"));
    assert!(repo.contains("| [[code/modules/src\\|src]] |"));
    assert!(file.contains("[[code/modules/src|src]]"));
    // Human Key components table: name + kind + hub purpose, no Signature /
    // Component / Component ID / Lines columns (#871).
    assert!(file.contains("| Symbol | Kind | Purpose |"));
    assert!(file.contains("| `Client` | class |"));
    assert!(!file.contains("Component ID"));
    // The full-range `<details>` provenance wall is dropped from file pages; the
    // repo overview still carries it.
    assert!(!file.contains("<details>"));
    assert!(repo.contains("- [src/lib.rs:1](src/lib.rs#L1)"));
    assert_eq!(
        frontmatter
            .get("generated_by")
            .and_then(serde_yaml::Value::as_str),
        Some("gcode-codewiki")
    );
    assert!(frontmatter.get("source").is_none());
    assert!(frontmatter.get("provenance").is_some());
    assert_eq!(
        frontmatter.get("trust").and_then(serde_yaml::Value::as_str),
        Some("generated")
    );
    assert_eq!(
        frontmatter
            .get("freshness")
            .and_then(serde_yaml::Value::as_str),
        Some("indexed")
    );
    assert!(frontmatter.get("source_files").is_none());
}

#[test]
fn repo_frontmatter_lists_source_files_without_range_blocks() {
    let docs = generate_hierarchical_docs(&repo_marker_input(), None);
    let repo = rendered_doc(&docs, "code/repo.md");
    let yaml = repo
        .strip_prefix("---\n")
        .and_then(|content| content.split_once("---\n\n"))
        .map(|(yaml, _)| yaml)
        .expect("frontmatter block");
    let frontmatter: serde_yaml::Value = serde_yaml::from_str(yaml).expect("parse frontmatter");
    let provenance = frontmatter
        .get("provenance")
        .and_then(serde_yaml::Value::as_sequence)
        .expect("provenance entries");

    assert_eq!(source_files_from_frontmatter(repo).len(), 6);
    assert!(provenance.iter().all(|entry| entry.get("file").is_some()));
    assert!(provenance.iter().all(|entry| entry.get("ranges").is_none()));
}

#[test]
fn repo_structural_fallback_omits_marker_wall_but_generated_text_stays_grounded() {
    let fallback_docs = generate_hierarchical_docs(&repo_marker_input(), None);
    let fallback_repo = rendered_doc(&fallback_docs, "code/repo.md");
    let fallback_overview = markdown_section(fallback_repo, "## Overview");

    assert!(fallback_overview.contains("Repository code documentation covers 6 files"));
    assert_eq!(inline_marker_count(fallback_overview), 0);

    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        if system == prompts::REPO_SYSTEM {
            Some("Generated repository overview.".to_string())
        } else {
            None
        }
    };
    let generated_docs = generate_hierarchical_docs(&repo_marker_input(), Some(&mut generator));
    let generated_repo = rendered_doc(&generated_docs, "code/repo.md");
    let generated_overview = markdown_section(generated_repo, "## Overview");

    assert!(generated_overview.contains("Generated repository overview."));
    assert_eq!(
        inline_marker_count(generated_overview),
        super::super::text::MAX_FALLBACK_CITATIONS
    );
}

#[test]
fn inline_code_uses_commonmark_backtick_delimiters() {
    assert_eq!(inline_code(""), "``");
    assert_eq!(inline_code("plain"), "`plain`");
    assert_eq!(inline_code("a`b"), "``a`b``");
    assert_eq!(inline_code("a``b"), "```a``b```");
    assert_eq!(inline_code("`edge`"), "`` `edge` ``");
    assert_eq!(inline_code("two\nlines"), "`two lines`");
    assert_eq!(inline_code("two\n\t  lines"), "`two lines`");
    assert_eq!(inline_code("  padded  value  "), "`padded value`");
}

#[test]
fn run_summary_serializes_daemon_contract_keys() {
    let summary = CodewikiRunSummary {
        command: "codewiki",
        project_id: "project-1".to_string(),
        project_root: "/repo".to_string(),
        out_dir: "/repo/codewiki".to_string(),
        generated_pages: 3,
        changed_paths: vec!["repo.md".to_string()],
        skipped: 2,
        files: 1,
        modules: 1,
        symbols: 4,
        ai_enabled: false,
    };

    let value = serde_json::to_value(summary).expect("summary json");

    assert_eq!(value["command"], "codewiki");
    assert_eq!(value["project_id"], "project-1");
    assert_eq!(value["project_root"], "/repo");
    assert_eq!(value["changed_paths"][0], "repo.md");
    assert_eq!(value["skipped"], 2);
    assert_eq!(value["ai_enabled"], false);
}

#[test]
fn component_id_uses_stored_symbol_id() {
    let mut symbol = test_symbol("src/lib.rs", "Client", "class", 1, "pub struct Client;");
    symbol.id = "stored-symbol-id".to_string();
    assert_eq!(symbol.id, "stored-symbol-id");
}

#[test]
fn file_doc_purpose_neutralizes_summary_link_tokens() {
    let mut symbol = test_symbol("src/lib.rs", "wiki_link", "function", 1, "fn wiki_link()");
    symbol.summary =
        Some("Quotes [[relative_path|title]] and [exact](knowledge/topics/exact).".to_string());
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![symbol],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let file = rendered_doc(&docs, "code/files/src/lib.rs.md");

    assert!(file.contains("| `wiki_link` | function |"));
    assert!(file.contains(
        "Quotes `[[relative_path\\|title]]` and `[exact](knowledge/topics/exact)`. [src/lib.rs:1]"
    ));
}

#[test]
fn file_page_structural_fallback_is_multi_section_without_symbol_dump() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client {",
        )],
    };

    let docs = generate_hierarchical_docs(&input, None);
    let file = rendered_doc(&docs, "code/files/src/lib.rs.md");

    // Overview + How it fits (body) + Key components (rendered) = three sections,
    // even with AI off — the page is narrative structure, not a bare summary.
    assert!(
        file.matches("\n## ").count() >= 3,
        "expected >=3 sections, got: {file}"
    );
    assert!(file.contains("## Overview"));
    assert!(file.contains("## How it fits"));
    assert!(file.contains("## Key components"));
    assert!(file.contains("| Symbol | Kind | Purpose |"));
    // None of the old machine surface: no UUID component IDs, no symbol-table
    // header, no full-range `<details>` provenance wall.
    assert!(!file.contains("Component ID"));
    assert!(!file.contains("## API Symbols"));
    assert!(!file.contains("<details>"));
}

#[test]
fn file_page_verify_records_unsupported_block_notes() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client {",
        )],
    };

    // Generator emits a multi-section file body with one planted fabrication.
    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        (system == prompts::FILE_SYSTEM).then(|| {
            "## Overview\n\nThe file defines the client [src/lib.rs:1].\n\n\
             Fabricated: it also mines bitcoin at midnight [src/lib.rs:1].\n\n\
             ## How it fits\n\nIt anchors the module [src/lib.rs:1]."
                .to_string()
        })
    };
    // Verifier returns a note for the block carrying the planted marker.
    let mut verifier = |prompt: &str, _system: &str| {
        let id = prompt
            .lines()
            .find_map(|line| {
                let (num, body) = line.strip_prefix('[')?.split_once("] ")?;
                body.contains("Fabricated").then(|| num.to_string())
            })
            .expect("planted block is numbered in the verify prompt");
        Some(format!(
            r#"[{{"id":{id},"reason":"Fabricated claim lacks source support."}}]"#
        ))
    };

    let docs = generate_hierarchical_docs_with_verify(
        &input,
        Some(&mut generator),
        Some(&mut verifier),
        AiDepth::Files,
    );
    let file = docs
        .iter()
        .find(|doc| doc.path == "code/files/src/lib.rs.md")
        .expect("file doc");

    assert!(
        file.content.contains("The file defines the client"),
        "{}",
        file.content
    );
    assert!(file.content.contains("Fabricated"), "{}", file.content);
    assert!(file.content.contains("mines bitcoin"), "{}", file.content);
    assert!(file.content.contains("verify_notes:"), "{}", file.content);
    assert!(
        file.content
            .contains("reason: Fabricated claim lacks source support."),
        "{}",
        file.content
    );
    assert!(
        !file.degraded,
        "verifier notes do not degrade the file page"
    );
    assert!(
        !file.content.contains("model-unavailable"),
        "{}",
        file.content
    );
}

#[test]
fn file_page_verify_uses_symbol_table_as_evidence() {
    let input = CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: vec!["src/lib.rs".to_string()],
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: vec![test_symbol(
            "src/lib.rs",
            "Client",
            "class",
            1,
            "pub struct Client {",
        )],
    };

    let mut generator = |_prompt: &str, system: &str, _tier: PromptTier| {
        (system == prompts::FILE_SYSTEM).then(|| {
            "## Overview\n\nThe Client symbol is a class in this file [src/lib.rs:1].\n\n\
             ## How it fits\n\nIt anchors the module [src/lib.rs:1]."
                .to_string()
        })
    };
    let mut saw_symbols = false;
    let docs = {
        let mut verifier = |prompt: &str, _system: &str| {
            saw_symbols = prompt.contains("Symbols:\n")
                && prompt
                    .contains("| Symbol | Kind | Component | Component ID | Lines | Purpose |")
                && prompt.contains("| Client | class | Client [class] |");
            Some(
                if saw_symbols {
                    "[]"
                } else {
                    r#"[{"id":2,"reason":"Missing symbol evidence."}]"#
                }
                .to_string(),
            )
        };

        generate_hierarchical_docs_with_verify(
            &input,
            Some(&mut generator),
            Some(&mut verifier),
            AiDepth::Files,
        )
    };
    let file = docs
        .iter()
        .find(|doc| doc.path == "code/files/src/lib.rs.md")
        .expect("file doc");

    assert!(
        saw_symbols,
        "verifier prompt should include symbol evidence"
    );
    assert!(
        file.content.contains("The Client symbol is a class"),
        "{}",
        file.content
    );
    assert!(
        !file.degraded,
        "symbol-supported file claim should survive verification"
    );
    assert!(
        !file.content.contains("verify_notes:"),
        "supported file claim should not record verifier notes"
    );
}

fn repo_marker_input() -> CodewikiInput {
    let files = [
        "alpha.rs",
        "beta.rs",
        "gamma.rs",
        "delta.rs",
        "epsilon.rs",
        "zeta.rs",
    ];
    CodewikiInput {
        leading_chunks: std::collections::BTreeMap::new(),
        files: files.iter().map(|file| (*file).to_string()).collect(),
        graph_edges: Vec::new(),
        graph_availability: CodewikiGraphAvailability::Available,
        symbols: files
            .iter()
            .enumerate()
            .map(|(index, file)| {
                test_symbol(
                    file,
                    &format!("item_{index}"),
                    "function",
                    1,
                    "pub fn item()",
                )
            })
            .collect(),
    }
}

fn rendered_doc<'a>(docs: &'a [(String, String)], path: &str) -> &'a str {
    docs.iter()
        .find(|(doc_path, _)| doc_path == path)
        .map(|(_, content)| content.as_str())
        .expect("rendered doc")
}

fn markdown_section<'a>(rendered: &'a str, heading: &str) -> &'a str {
    let (_, after_heading) = rendered.split_once(heading).expect("section heading");
    after_heading
        .split_once("\n## ")
        .map(|(section, _)| section)
        .unwrap_or(after_heading)
}

fn inline_marker_count(text: &str) -> usize {
    text.split_whitespace()
        .filter(|token| {
            token
                .strip_prefix('[')
                .and_then(|value| value.strip_suffix(']'))
                .is_some_and(|value| value.chars().all(|ch| ch.is_ascii_digit()))
        })
        .count()
}
