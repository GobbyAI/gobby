use std::path::{Path, PathBuf};
use std::sync::Arc;

use super::claims::{
    claim_lines, has_codewiki_frontmatter_source_spans, has_inline_source_support,
    unsupported_claims,
};
use super::*;
use crate::lint::WikiPage;
use crate::provenance::ProvenanceGraph;
use crate::sources::{SourceDraft, SourceManifest};

#[test]
fn reports_unsupported_claims() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let source = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/source",
            "2026-05-29T12:00:00Z",
            "source body",
        )
        .with_citation("Example Source"),
    )
    .expect("source registered");
    let page = root.join("knowledge/topics/claims.md");
    std::fs::create_dir_all(page.parent().expect("page parent")).expect("create wiki dir");
    std::fs::write(
        &page,
        "---\ntitle: Claims\nsource_kind: topic\n---\n# Claims\nUnsupported operational claim.\n",
    )
    .expect("write page");

    let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");

    assert_eq!(report.unsupported_claims.len(), 1);
    let claim = &report.unsupported_claims[0];
    assert_eq!(claim.path, PathBuf::from("knowledge/topics/claims.md"));
    assert_eq!(claim.line, 6);
    assert_eq!(claim.heading.as_deref(), Some("Claims"));
    assert!(claim.claim.contains("Unsupported operational claim"));
    assert_eq!(claim.source_context[0].source_id, source.id);
    assert_eq!(
        claim.source_context[0].citation.as_deref(),
        Some("Example Source")
    );
}

#[test]
fn generated_codewiki_numeric_claims_do_not_inherit_raw_source_context() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let source = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/raw-source",
            "2026-05-29T12:00:00Z",
            "raw source body",
        )
        .with_citation("Raw Source"),
    )
    .expect("source registered");
    let code_page = root.join("code/_changes.md");
    std::fs::create_dir_all(code_page.parent().expect("code parent")).expect("create code dir");
    std::fs::write(
        &code_page,
        "---\ntitle: Index Changes\nkind: code_changes\ngenerated_by: gcode-codewiki\ntrust: generated\nfreshness: indexed\n---\n# Index Changes\n\n## Current Snapshot\n\n- Files: 457\n- Symbols: 7901\n",
    )
    .expect("write code page");
    let knowledge_page = root.join("knowledge/topics/claims.md");
    std::fs::create_dir_all(knowledge_page.parent().expect("knowledge parent"))
        .expect("create knowledge dir");
    std::fs::write(
        &knowledge_page,
        "---\ntitle: Claims\nsource_kind: topic\n---\n# Claims\nUnsupported operational claim.\n",
    )
    .expect("write knowledge page");

    let report = run(root, ScopeIdentity::project("project-123")).expect("audit runs");

    let code_path = PathBuf::from("code/_changes.md");
    let generated_claims = report
        .unsupported_claims
        .iter()
        .filter(|claim| claim.path.as_path() == code_path.as_path())
        .collect::<Vec<_>>();
    assert_eq!(generated_claims.len(), 2);
    assert!(
        generated_claims
            .iter()
            .any(|claim| claim.claim == "Files: 457")
    );
    assert!(
        generated_claims
            .iter()
            .any(|claim| claim.claim == "Symbols: 7901")
    );
    assert!(
        generated_claims
            .iter()
            .all(|claim| claim.source_context.is_empty())
    );
    let rendered = render_text(&report);
    assert!(
        rendered
            .lines()
            .filter(|line| line.contains("code/_changes.md"))
            .all(|line| !line.contains("[sources:"))
    );
    let knowledge_claim = report
        .unsupported_claims
        .iter()
        .find(|claim| claim.path == Path::new("knowledge/topics/claims.md"))
        .expect("knowledge claim");
    assert_eq!(knowledge_claim.source_context[0].source_id, source.id);
}

#[test]
fn reports_include_paths_and_scope() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let page = root.join("knowledge/topics/path-scope.md");
    std::fs::create_dir_all(page.parent().expect("page parent")).expect("create wiki dir");
    std::fs::write(
        &page,
        "---\ntitle: Path Scope\nsource_kind: topic\n---\n# Path Scope\nClaim needing evidence.\n",
    )
    .expect("write page");

    let report = run(root, ScopeIdentity::project("project-123")).expect("audit runs");
    let json = serde_json::to_value(&report).expect("report serializes");

    assert_eq!(report.scope, ScopeIdentity::project("project-123"));
    assert_eq!(
        json.pointer("/scope/id")
            .and_then(serde_json::Value::as_str),
        Some("project-123")
    );
    assert_eq!(
        json.pointer("/unsupported_claims/0/path")
            .and_then(serde_json::Value::as_str),
        Some("knowledge/topics/path-scope.md")
    );
}

#[test]
fn topic_scope_audits_only_topic_pages() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let topic_page = root.join("knowledge/topics/topic-claim.md");
    let concept_page = root.join("knowledge/concepts/concept-claim.md");
    std::fs::create_dir_all(topic_page.parent().expect("topic parent")).expect("create topic dir");
    std::fs::create_dir_all(concept_page.parent().expect("concept parent"))
        .expect("create concept dir");
    std::fs::write(
        &topic_page,
        "---\ntitle: Topic\nsource_kind: topic\n---\n# Topic\nTopic claim.\n",
    )
    .expect("write topic page");
    std::fs::write(
        &concept_page,
        "---\ntitle: Concept\nsource_kind: concept\n---\n# Concept\nConcept claim.\n",
    )
    .expect("write concept page");

    let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");

    assert_eq!(report.unsupported_claims.len(), 1);
    assert_eq!(
        report.unsupported_claims[0].path,
        PathBuf::from("knowledge/topics/topic-claim.md")
    );
}

#[test]
fn frontmatter_closes_only_on_matching_document_start_delimiter() {
    let page = WikiPage {
        path: PathBuf::from("knowledge/topics/frontmatter.md"),
        relative_path: PathBuf::from("knowledge/topics/frontmatter.md"),
        markdown: "+++\ntitle = \"Frontmatter\"\n---\nstill_frontmatter = true\n+++\n# Body\nClaim after TOML frontmatter.\n---\nClaim after thematic break.\n".to_string(),
        parsed: crate::markdown::parse_markdown(
            "knowledge/topics/frontmatter.md",
            "# Body\n",
            std::iter::empty::<&str>(),
        )
        .expect("parse markdown"),
        has_frontmatter: true,
    };

    let claims = claim_lines(&page, &AuditOptions::default());

    assert_eq!(claims.len(), 2);
    assert_eq!(claims[0].text, "Claim after TOML frontmatter.");
    assert_eq!(claims[1].text, "Claim after thematic break.");
}

#[test]
fn multiline_html_comments_do_not_emit_claims() {
    let page = WikiPage {
        path: PathBuf::from("knowledge/topics/comments.md"),
        relative_path: PathBuf::from("knowledge/topics/comments.md"),
        markdown: "# Comments\nVisible claim.\n<!--\nHidden claim.\n-->\nAfter claim.\n"
            .to_string(),
        parsed: crate::markdown::parse_markdown(
            "knowledge/topics/comments.md",
            "# Comments\n",
            std::iter::empty::<&str>(),
        )
        .expect("parse markdown"),
        has_frontmatter: false,
    };

    let claims = claim_lines(&page, &AuditOptions::default());

    assert_eq!(claims.len(), 2);
    assert_eq!(claims[0].text, "Visible claim.");
    assert_eq!(claims[1].text, "After claim.");
}

#[test]
fn inline_source_support_requires_link_like_target() {
    assert!(!has_inline_source_support("the upstream source: TBD"));
    assert!(has_inline_source_support(
        "Evidence source: https://example.com/report"
    ));
    assert!(has_inline_source_support(
        "Evidence citation: [[knowledge/sources/source-1]]"
    ));
}

#[test]
fn inline_source_support_accepts_codewiki_source_spans() {
    assert!(has_inline_source_support(
        "Purpose: documents the builder. [crates/gcode/src/commands/codewiki/build.rs:3-73]"
    ));
    assert!(has_inline_source_support(
        "Root metadata is loaded from [Cargo.toml:1]"
    ));
    assert!(!has_inline_source_support(
        "Not a source citation [placeholder:123]"
    ));
    assert!(!has_inline_source_support(
        "Invalid range [crates/gwiki/src/audit.rs:42-3]"
    ));
}

#[test]
fn codewiki_frontmatter_source_spans_support_structural_claims() {
    let markdown = r#"---
title: crates/example.rs
type: code_file
provenance:
- file: crates/example.rs
  ranges:
  - 1-12
---

# crates/example.rs

Module: [[code/modules/crates|crates]]
Signature: `fn example() -> bool {`
"#;
    let page = WikiPage {
        path: PathBuf::from("code/files/crates/example.rs.md"),
        relative_path: PathBuf::from("code/files/crates/example.rs.md"),
        markdown: markdown.to_string(),
        parsed: crate::markdown::parse_markdown(
            "code/files/crates/example.rs.md",
            markdown,
            std::iter::empty::<&str>(),
        )
        .expect("parse markdown"),
        has_frontmatter: true,
    };
    assert!(has_codewiki_frontmatter_source_spans(&page));

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &AuditOptions::default(),
    );

    assert!(claims.is_empty());
}

#[test]
fn codewiki_contract_golden_page_counts_as_code_source_spans() {
    let page = test_codewiki_page(
        "code/files/src/lib.rs.md",
        gobby_core::codewiki_contract::GOLDEN_PAGE,
    );

    assert!(has_codewiki_frontmatter_source_spans(&page));
}

#[test]
fn codewiki_frontmatter_source_spans_do_not_support_prose_claims() {
    let markdown = r#"---
title: crates/example.rs
type: code_file
provenance:
- file: crates/example.rs
  ranges:
  - 1-12
---

# crates/example.rs

Module: [[code/modules/crates|crates]]
This generated page makes an unsupported prose claim.
"#;
    let page = test_codewiki_page("code/files/crates/example.rs.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &AuditOptions::default(),
    );

    assert_eq!(claims.len(), 1);
    assert_eq!(
        claims[0].claim,
        "This generated page makes an unsupported prose claim."
    );
}

#[test]
fn frontmatter_migration_audits_legacy_and_shared_sources_equivalently() {
    let legacy = r#"---
title: crates/example.rs
source_files:
- file: crates/example.rs
  ranges:
  - 1-12
---

# crates/example.rs

Signature: `fn example() -> bool {`
"#;
    let canonical = r#"---
title: crates/example.rs
provenance:
- file: crates/example.rs
  ranges:
  - 1-12
generated_by: gcode-codewiki
trust: generated
freshness: indexed
---

# crates/example.rs

Signature: `fn example() -> bool {`
"#;

    let legacy_page = test_codewiki_page("code/files/crates/example.rs.md", legacy);
    let canonical_page = test_codewiki_page("code/files/crates/example.rs.md", canonical);

    assert!(!has_codewiki_frontmatter_source_spans(&legacy_page));
    assert!(has_codewiki_frontmatter_source_spans(&canonical_page));
    assert_eq!(
        unsupported_claims(
            &legacy_page,
            &ProvenanceGraph::default(),
            &Arc::new(Vec::new()),
            &AuditOptions::default(),
        )
        .len(),
        1
    );
    assert!(
        unsupported_claims(
            &canonical_page,
            &ProvenanceGraph::default(),
            &Arc::new(Vec::new()),
            &AuditOptions::default(),
        )
        .is_empty()
    );
}

fn test_codewiki_page(path: &str, markdown: &str) -> WikiPage {
    WikiPage {
        path: PathBuf::from(path),
        relative_path: PathBuf::from(path),
        markdown: markdown.to_string(),
        parsed: crate::markdown::parse_markdown(path, markdown, std::iter::empty::<&str>())
            .expect("parse markdown"),
        has_frontmatter: true,
    }
}

#[test]
fn configured_ignored_sections_extend_defaults() {
    let page = WikiPage {
        path: PathBuf::from("knowledge/topics/release.md"),
        relative_path: PathBuf::from("knowledge/topics/release.md"),
        markdown: "# Release\nClaim needing support.\n## Notes\nIgnored internal note.\n## Sources\nIgnored source note.\n".to_string(),
        parsed: crate::markdown::parse_markdown(
            "knowledge/topics/release.md",
            "# Release\n",
            std::iter::empty::<&str>(),
        )
        .expect("parse markdown"),
        has_frontmatter: false,
    };
    let options = AuditOptions::default().with_additional_ignored_sections(["Notes"]);

    let claims = claim_lines(&page, &options);

    assert_eq!(claims.len(), 1);
    assert_eq!(claims[0].text, "Claim needing support.");
}
