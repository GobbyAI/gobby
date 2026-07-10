use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use super::claims::{
    analyze_claims, claim_lines, has_codewiki_frontmatter_source_spans, has_inline_source_support,
    is_manifest_backed_source_digest,
};
use super::*;
use crate::lint::WikiPage;
use crate::provenance::ProvenanceGraph;
use crate::sources::{SourceDraft, SourceManifest};

/// Unsupported-claims view of [`analyze_claims`] for tests that only assert
/// on audit failures.
fn unsupported_claims(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    source_context: &Arc<Vec<AuditSourceContext>>,
    manifest_hashes: &BTreeSet<String>,
    options: &AuditOptions,
) -> Vec<UnsupportedClaim> {
    analyze_claims(page, provenance, source_context, manifest_hashes, options).unsupported
}

#[test]
fn catalog_surfaces_are_exempt_from_claim_scanning() {
    // The deterministic catalog files rebuilt by `catalog::regenerate` are
    // navigation artifacts; their placeholder prose must not flag, while the
    // identical text on any other page still does.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let body = "# Code\n\n## Handbook\n\n(none yet)\n";
    for relative in [
        "_index.md",
        "knowledge/INDEX.md",
        "code/INDEX.md",
        "knowledge/concepts/_context.md",
    ] {
        let page = root.join(relative);
        std::fs::create_dir_all(page.parent().expect("page parent")).expect("create wiki dir");
        std::fs::write(&page, body).expect("write catalog page");
    }
    let control = root.join("knowledge/topics/placeholder.md");
    std::fs::create_dir_all(control.parent().expect("page parent")).expect("create wiki dir");
    std::fs::write(&control, body).expect("write control page");

    let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");

    assert_eq!(
        report.unsupported_claims.len(),
        1,
        "only the non-catalog page flags: {:?}",
        report.unsupported_claims
    );
    assert_eq!(
        report.unsupported_claims[0].path,
        PathBuf::from("knowledge/topics/placeholder.md")
    );
}

#[test]
fn recap_pages_are_exempt_from_claim_scanning() {
    // Daily recaps are page-level supported (#17575): the deterministic
    // session listing is structural and the overview grounds on the day's
    // digests. The same prose on a non-recap page still flags.
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let body = "# Recap\n\n## Overview\n\nSynthesis was unavailable for this run.\n";
    let recap = root.join("recaps/2026-07-05.md");
    std::fs::create_dir_all(recap.parent().expect("recap parent")).expect("create recaps dir");
    std::fs::write(
        &recap,
        format!("---\ntitle: \"Recap: 2026-07-05\"\nrecap_date: 2026-07-05\n---\n{body}"),
    )
    .expect("write recap page");
    let control = root.join("knowledge/topics/not-a-recap.md");
    std::fs::create_dir_all(control.parent().expect("control parent")).expect("create topics dir");
    std::fs::write(&control, body).expect("write control page");

    let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");

    assert_eq!(
        report.unsupported_claims.len(),
        1,
        "only the non-recap page flags: {:?}",
        report.unsupported_claims
    );
    assert_eq!(
        report.unsupported_claims[0].path,
        PathBuf::from("knowledge/topics/not-a-recap.md")
    );
}

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
    // A codewiki-generated `code/**` projection page (index-count claims like
    // `Files: 457`/`Symbols: 7901`) is exempt from claim scanning: it must
    // contribute no unsupported claims and, in particular, must never inherit
    // raw source context from unrelated knowledge sources.
    assert!(
        generated_claims.is_empty(),
        "generated code projection page should be exempt, got {generated_claims:?}"
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
        &BTreeSet::new(),
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
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert_eq!(claims.len(), 1);
    assert_eq!(
        claims[0].claim,
        "This generated page makes an unsupported prose claim."
    );
}

#[test]
fn template_projected_code_docs_are_page_level_supported() {
    // Mirrors a real `ai_route: off` codewiki file page: file-level provenance,
    // no per-claim ranges, and purely mechanical scaffolding (overview count,
    // symbol/kind rows, module wikilink). Every line is index-derived and
    // attributed to the declared source file, so nothing is unsupported even
    // though none of the lines carry inline or frontmatter source spans.
    let markdown = r#"---
title: src/mathutil.py
type: code_file
provenance:
- file: src/mathutil.py
generated_by: gcode-codewiki
trust: generated
freshness: indexed
ai_route: off
ai_fallback: false
ai_generation_status: skipped
---

# src/mathutil.py

Module: [[code/modules/src|src]]

## Overview

`src/mathutil.py` exposes 1 indexed API symbol.

## Reference

- Symbol: `add`
  Kind: function
"#;
    let page = test_codewiki_page("code/files/src/mathutil.py.md", markdown);

    // Guard the precondition: this page has no per-claim source spans, so the
    // exemption is the only thing that can credit its claims.
    assert!(!has_codewiki_frontmatter_source_spans(&page));

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert!(
        claims.is_empty(),
        "template-projected code doc should have no unsupported claims, got {claims:?}"
    );
}

#[test]
fn degraded_codewiki_docs_are_page_level_supported() {
    // A degraded page fell back to the template body (no AI narrative landed),
    // so it is credited wholesale just like a skipped page.
    let markdown = r#"---
title: src/mathutil.py
type: code_file
provenance:
- file: src/mathutil.py
generated_by: gcode-codewiki
trust: generated
freshness: indexed
ai_route: off
ai_fallback: true
ai_generation_status: degraded
degraded: true
degraded_sources:
- model_provider_unavailable
---

# src/mathutil.py

`src/mathutil.py` exposes 1 indexed API symbol.
"#;
    let page = test_codewiki_page("code/files/src/mathutil.py.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert!(
        claims.is_empty(),
        "degraded template code doc should have no unsupported claims, got {claims:?}"
    );
}

#[test]
fn generated_status_code_docs_are_grounded_by_file_provenance() {
    // A code page with landed AI narrative (ai_generation_status: generated) is
    // still grounded by its file provenance: the narrative is about that one
    // indexed file, and the per-line claim model cannot meaningfully audit
    // flowing prose. It is exempt, exactly like a template-projected page.
    let markdown = r#"---
title: src/mathutil.py
type: code_file
provenance:
- file: src/mathutil.py
generated_by: gcode-codewiki
trust: generated
freshness: indexed
ai_route: daemon
ai_fallback: false
ai_generation_status: generated
---

# src/mathutil.py

This narrative sentence describes the file and is grounded by its provenance.
"#;
    let page = test_codewiki_page("code/files/src/mathutil.py.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert!(
        claims.is_empty(),
        "generated-status code doc with file provenance should be grounded, got {claims:?}"
    );
}

#[test]
fn aggregate_code_pages_with_empty_provenance_are_exempt() {
    // The `code/repo.md` aggregate rolls up the whole tree and carries no
    // per-file frontmatter provenance (`provenance: []`), but it is still a
    // codewiki index projection (generated_by + trust: generated), so its
    // mechanical module counts and links are exempt.
    let markdown = r#"---
title: Repository Overview
type: code_repo
provenance: []
generated_by: gcode-codewiki
trust: generated
freshness: indexed
stub: true
ai_route: off
ai_fallback: false
ai_generation_status: skipped
---

# Repository Overview

Repository code documentation covers 2553 files across 275 modules.

Module: [[code/modules/crates|crates]]
Summary: `crates` contains 0 direct files and 4 child modules.
"#;
    let page = test_codewiki_page("code/repo.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert!(
        claims.is_empty(),
        "aggregate code projection page should be exempt, got {claims:?}"
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
            &BTreeSet::new(),
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
            &BTreeSet::new(),
            &AuditOptions::default(),
        )
        .is_empty()
    );
}

#[test]
fn manifest_backed_source_digest_is_page_level_supported() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let record = SourceManifest::register(
        root,
        SourceDraft::url(
            "https://example.com/digest",
            "2026-05-29T12:00:00Z",
            "digest body",
        )
        .with_citation("Digest Source"),
    )
    .expect("source registered");

    let page = root.join(format!("knowledge/sources/{}.md", record.id));
    std::fs::create_dir_all(page.parent().expect("page parent")).expect("create sources dir");
    std::fs::write(
        &page,
        format!(
            "---\ntitle: Digest\nsource_kind: url\nsource_hash: {}\n---\n# Digest\nUncited prose claim from the digest.\nSignature: `fn example() -> bool {{`\n",
            record.content_hash
        ),
    )
    .expect("write digest page");

    let report = run(root, ScopeIdentity::project("project-123")).expect("audit runs");

    let digest_path = PathBuf::from(format!("knowledge/sources/{}.md", record.id));
    assert!(
        report
            .unsupported_claims
            .iter()
            .all(|claim| claim.path != digest_path),
        "manifest-backed digest claims should be page-level supported"
    );
}

#[test]
fn source_digest_without_manifest_match_is_still_audited() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let orphan = root.join("knowledge/sources/orphan.md");
    std::fs::create_dir_all(orphan.parent().expect("page parent")).expect("create sources dir");
    std::fs::write(
        &orphan,
        "---\ntitle: Orphan\nsource_kind: url\nsource_hash: not-a-registered-hash\n---\n# Orphan\nUncited prose claim with an orphan hash.\n",
    )
    .expect("write orphan digest");
    let hashless = root.join("knowledge/sources/hashless.md");
    std::fs::write(
        &hashless,
        "---\ntitle: Hashless\nsource_kind: url\n---\n# Hashless\nUncited prose claim without a source hash.\n",
    )
    .expect("write hashless digest");

    let report = run(root, ScopeIdentity::project("project-123")).expect("audit runs");

    assert!(
        report
            .unsupported_claims
            .iter()
            .any(|claim| claim.path == Path::new("knowledge/sources/orphan.md")),
        "orphan-hash digest should still be audited"
    );
    assert!(
        report
            .unsupported_claims
            .iter()
            .any(|claim| claim.path == Path::new("knowledge/sources/hashless.md")),
        "digest without a source hash should still be audited"
    );
}

#[test]
fn manifest_backed_digest_exemption_requires_sources_path_and_covers_all_kinds() {
    let markdown = "---\ntitle: Digest\nsource_kind: session\nsource_hash: hash-1\n---\n# Digest\nUncited prose claim from the digest.\nSignature: `fn example() -> bool {`\n";
    let digest = test_codewiki_page("knowledge/sources/src-1.md", markdown);
    let topic = test_codewiki_page("knowledge/topics/src-1.md", markdown);
    let hashes = BTreeSet::from(["hash-1".to_string()]);

    assert!(is_manifest_backed_source_digest(&digest, &hashes));
    assert!(!is_manifest_backed_source_digest(&topic, &hashes));
    assert!(!is_manifest_backed_source_digest(&digest, &BTreeSet::new()));

    let exempted = unsupported_claims(
        &digest,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &hashes,
        &AuditOptions::default(),
    );
    assert!(
        exempted.is_empty(),
        "prose and structural claims are exempt"
    );

    let unmatched = unsupported_claims(
        &digest,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );
    assert!(
        !unmatched.is_empty(),
        "no manifest match means no exemption"
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

#[test]
fn source_excerpts_section_is_ignored() {
    // Daemon-synthesis concept pages quote verbatim source excerpts under a
    // `## Source excerpts` heading; each line names its session and is source
    // material, not a synthesized claim (#17704).
    let page = test_codewiki_page(
        "knowledge/concepts/example-concept.md",
        "---\ntitle: Example\nsource_kind: concept\n---\n# Example\n## Source excerpts\n- Session: 019d79c6 — 2026-04-10: # Session heading\n",
    );

    let claims = claim_lines(&page, &AuditOptions::default());

    assert!(
        claims.is_empty(),
        "source-excerpt lines should not be claims, got {claims:?}"
    );
}

#[test]
fn daemon_synthesis_concept_overview_is_credited_by_section_attribution() {
    // A daemon-synthesis concept page attributes its Overview synthesis to the
    // source with a trailing `_Source: [[knowledge/sources/...]]_` line. That
    // section-level attribution must credit the wrapped prose lines that carry
    // no inline citation of their own (#17704).
    let markdown = "---\ntitle: Claude\nsource_kind: concept\nsynthesis_mode: daemon\n---\n# Claude\n## Overview\nClaude is one of the providers investigated during runtime discovery.\n\nThe source also says Claude probing uses hardcoded shortnames.\n\n_Source: [[knowledge/sources/src-93b4c1e401d23dd7-session-019d79c6-9d23-7180-a50b-2622488b0c47|Session]]_\n";
    let page = test_codewiki_page("knowledge/concepts/claude-concept.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert!(
        claims.is_empty(),
        "daemon-synthesis Overview prose should be credited by its section attribution, got {claims:?}"
    );
}

#[test]
fn curated_concept_prose_without_daemon_synthesis_stays_audited() {
    // The section-attribution crediting is scoped to auto-generated
    // daemon-synthesis pages (`synthesis_mode: daemon`). An otherwise identical
    // page without that marker keeps per-claim auditing, so its uncited prose is
    // still flagged even though the section carries a trailing source line.
    let markdown = "---\ntitle: Claude\nsource_kind: concept\n---\n# Claude\n## Overview\nAn uncited curated assertion about Claude.\n\n_Source: [[knowledge/sources/src-93b4c1e401d23dd7-session-019d79c6-9d23-7180-a50b-2622488b0c47|Session]]_\n";
    let page = test_codewiki_page("knowledge/concepts/claude.md", markdown);

    let claims = unsupported_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert_eq!(
        claims.len(),
        1,
        "curated (non-daemon) prose without an inline citation should stay flagged, got {claims:?}"
    );
    assert!(claims[0].claim.contains("uncited curated assertion"));
}

#[test]
fn classifies_extracted_inferred_and_ambiguous_claims() {
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

Documents the builder pipeline. [crates/example.rs:3-9]

Uncited operational assertion about the builder.
"#;
    let page = test_codewiki_page("code/files/crates/example.rs.md", markdown);

    let analysis = analyze_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    let classification_for = |needle: &str| {
        analysis
            .classified
            .iter()
            .find(|claim| claim.claim.contains(needle))
            .unwrap_or_else(|| panic!("claim containing {needle:?} in {:?}", analysis.classified))
            .classification
    };
    assert_eq!(
        classification_for("Module:"),
        ClaimClassification::Inferred,
        "structural claim on a source-span-grounded page is inferred"
    );
    assert_eq!(
        classification_for("Documents the builder"),
        ClaimClassification::Extracted,
        "inline code source span is a direct citation"
    );
    assert_eq!(
        classification_for("Uncited operational assertion"),
        ClaimClassification::Ambiguous,
        "prose without provenance is ambiguous"
    );
    assert_eq!(
        analysis.unsupported.len(),
        1,
        "only the ambiguous prose claim is an audit failure, got {:?}",
        analysis.unsupported
    );
    assert!(
        analysis.unsupported[0]
            .claim
            .contains("Uncited operational assertion")
    );
}

#[test]
fn conflict_and_gap_sections_classify_ambiguous_without_audit_failures() {
    let markdown = r#"---
title: Topic
source_kind: topic
---
# Topic

## Conflicting claims

- Source A says the cache is per-project; source B says it is global.

## Missing evidence

- No source covers the shutdown path.
"#;
    let page = test_codewiki_page("knowledge/topics/conflicts.md", markdown);

    let analysis = analyze_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    let flagged: Vec<_> = analysis
        .classified
        .iter()
        .filter(|claim| {
            matches!(
                claim.heading.as_deref(),
                Some("Conflicting claims") | Some("Missing evidence")
            )
        })
        .collect();
    assert_eq!(
        flagged.len(),
        2,
        "conflict/gap claims enter the classified stream, got {:?}",
        analysis.classified
    );
    assert!(
        flagged
            .iter()
            .all(|claim| claim.classification == ClaimClassification::Ambiguous),
        "conflict/gap territory classifies ambiguous, got {flagged:?}"
    );
    assert!(
        analysis.unsupported.is_empty(),
        "explicitly flagged uncertainty is not an audit failure, got {:?}",
        analysis.unsupported
    );
}

#[test]
fn conflict_prefixed_claims_under_normal_headings_stay_unsupported() {
    let markdown = r#"---
title: Topic
source_kind: topic
---
# Topic

conflict: retention window is 30 days vs 90 days across sources.
"#;
    let page = test_codewiki_page("knowledge/topics/prefixed.md", markdown);

    let analysis = analyze_claims(
        &page,
        &ProvenanceGraph::default(),
        &Arc::new(Vec::new()),
        &BTreeSet::new(),
        &AuditOptions::default(),
    );

    assert_eq!(analysis.classified.len(), 1);
    assert_eq!(
        analysis.classified[0].classification,
        ClaimClassification::Ambiguous,
        "conflict-prefixed line is ambiguous territory"
    );
    assert_eq!(
        analysis.unsupported.len(),
        1,
        "a conflict-prefixed line under a normal heading still needs support"
    );
}

#[test]
fn audit_report_serializes_uppercase_claim_classifications() {
    let temp = tempfile::tempdir().expect("tempdir");
    let root = temp.path();
    let page = root.join("knowledge/topics/claims.md");
    std::fs::create_dir_all(page.parent().expect("page parent")).expect("create wiki dir");
    std::fs::write(
        &page,
        "---\ntitle: Claims\nsource_kind: topic\n---\n# Claims\nUnsupported operational claim.\n",
    )
    .expect("write page");

    let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");
    assert_eq!(report.claims.len(), 1);
    assert_eq!(
        report.claims[0].classification,
        ClaimClassification::Ambiguous
    );

    let json = serde_json::to_value(&report).expect("serialize report");
    assert_eq!(
        json.pointer("/claims/0/classification"),
        Some(&serde_json::Value::String("AMBIGUOUS".to_string()))
    );
}
