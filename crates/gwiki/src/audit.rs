use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;
use serde_json::Value;

use crate::lint::{WikiPage, collect_pages, line_number};
use crate::markdown::{
    MarkdownFence, markdown_fence_closes, markdown_fence_start, parse_atx_heading,
};
use crate::provenance::ProvenanceGraph;
use crate::sources::SourceManifest;
use crate::support::scope::scope_includes_page;
use crate::synthesis::slugify;
use crate::{ScopeIdentity, WikiError};

const DEFAULT_IGNORED_SECTIONS: &[&str] = &[
    "citations",
    "citation",
    "sources",
    "source",
    "backlinks",
    "extracts",
    "used by",
    "missing evidence",
    "conflicting claims",
];

const IGNORED_SECTIONS_ENV: &str = "GOBBY_WIKI_AUDIT_IGNORED_SECTIONS";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuditOptions {
    ignored_sections: BTreeSet<String>,
}

impl AuditOptions {
    pub fn from_env() -> Self {
        let mut options = Self::default();
        if let Ok(value) = std::env::var(IGNORED_SECTIONS_ENV) {
            options.extend_ignored_sections(value.split(','));
        }
        options
    }

    #[allow(dead_code, reason = "reserved gwiki CLI/API split")]
    pub fn with_additional_ignored_sections<I, S>(mut self, sections: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.extend_ignored_sections(sections);
        self
    }

    fn ignores_section(&self, heading: &str) -> bool {
        self.ignored_sections
            .contains(&heading.trim().to_ascii_lowercase())
    }

    fn extend_ignored_sections<I, S>(&mut self, sections: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.ignored_sections.extend(
            sections
                .into_iter()
                .map(|section| section.as_ref().trim().to_ascii_lowercase())
                .filter(|section| !section.is_empty()),
        );
    }
}

impl Default for AuditOptions {
    fn default() -> Self {
        Self {
            ignored_sections: DEFAULT_IGNORED_SECTIONS
                .iter()
                .map(|section| section.to_string())
                .collect(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuditReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub root: PathBuf,
    pub unsupported_claims: Vec<UnsupportedClaim>,
    pub source_context: Arc<Vec<AuditSourceContext>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct UnsupportedClaim {
    pub path: PathBuf,
    pub line: usize,
    pub heading: Option<String>,
    pub claim: String,
    pub reason: String,
    pub source_context: Arc<Vec<AuditSourceContext>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AuditSourceContext {
    pub source_id: String,
    pub path: Option<PathBuf>,
    pub citation: Option<String>,
    pub location: Option<String>,
}

#[allow(dead_code, reason = "reserved gwiki CLI/API split")]
pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<AuditReport, WikiError> {
    run_with_options(vault_root, scope, AuditOptions::from_env())
}

pub fn run_with_options(
    vault_root: &Path,
    scope: ScopeIdentity,
    options: AuditOptions,
) -> Result<AuditReport, WikiError> {
    let pages = collect_pages(vault_root)?
        .into_iter()
        .filter(|page| scope_includes_page(&scope, &page.relative_path))
        .collect::<Vec<_>>();
    let source_context = Arc::new(source_context(vault_root)?);
    let provenance = load_provenance(vault_root)?;
    let unsupported_claims = pages
        .iter()
        .flat_map(|page| unsupported_claims(page, &provenance, &source_context, &options))
        .collect();

    Ok(AuditReport {
        command: "audit",
        scope,
        root: vault_root.to_path_buf(),
        unsupported_claims,
        source_context,
    })
}

pub fn render_text(report: &AuditReport) -> String {
    let mut text = format!("Wiki audit report\nScope: {}\n", report.scope);
    text.push_str("\nUnsupported claims:\n");
    if report.unsupported_claims.is_empty() {
        text.push_str("- none\n");
    } else {
        for claim in &report.unsupported_claims {
            text.push_str("- ");
            text.push_str(&claim.path.display().to_string());
            text.push(':');
            text.push_str(&claim.line.to_string());
            text.push(' ');
            text.push_str(&claim.claim);
            if !claim.source_context.is_empty() {
                text.push_str(" [sources: ");
                text.push_str(
                    &claim
                        .source_context
                        .iter()
                        .map(|source| source.source_id.clone())
                        .collect::<Vec<_>>()
                        .join(", "),
                );
                text.push(']');
            }
            text.push('\n');
        }
    }
    text
}

fn source_context(vault_root: &Path) -> Result<Vec<AuditSourceContext>, WikiError> {
    let manifest = SourceManifest::read(vault_root)?;
    Ok(manifest
        .entries
        .into_iter()
        .map(|entry| AuditSourceContext {
            path: Some(PathBuf::from("raw").join(format!("{}.md", entry.id))),
            source_id: entry.id,
            citation: entry.citation,
            location: Some(entry.location),
        })
        .collect())
}

fn load_provenance(vault_root: &Path) -> Result<ProvenanceGraph, WikiError> {
    let path = vault_root.join("meta").join("provenance.json");
    if path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)
    } else {
        Ok(ProvenanceGraph::default())
    }
}

fn unsupported_claims(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    source_context: &Arc<Vec<AuditSourceContext>>,
    options: &AuditOptions,
) -> Vec<UnsupportedClaim> {
    let claims = claim_lines(page, options);
    let supported_lines = supported_claim_lines(page, provenance, &claims);
    let has_page_source_support = has_codewiki_frontmatter_source_spans(page);
    let claim_source_context = claim_source_context(page, source_context);
    claims
        .into_iter()
        .filter_map(|claim| {
            if (has_page_source_support && claim.kind == ClaimKind::Structural)
                || supported_lines.contains(&claim.line)
                || has_inline_source_support(&claim.text)
            {
                return None;
            }
            Some(UnsupportedClaim {
                path: page.relative_path.clone(),
                line: claim.line,
                heading: claim.heading,
                claim: claim.text,
                reason: "claim has no source provenance or inline citation".to_string(),
                source_context: Arc::clone(&claim_source_context),
            })
        })
        .collect()
}

fn claim_source_context(
    page: &WikiPage,
    source_context: &Arc<Vec<AuditSourceContext>>,
) -> Arc<Vec<AuditSourceContext>> {
    if is_generated_codewiki_page(page) {
        Arc::new(Vec::new())
    } else {
        Arc::clone(source_context)
    }
}

fn is_generated_codewiki_page(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    page_path.starts_with("code/")
        && page.parsed.frontmatter.trust.as_deref()
            == Some(gobby_core::codewiki_contract::TRUST_GENERATED)
}

fn has_codewiki_frontmatter_source_spans(page: &WikiPage) -> bool {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    page_path.starts_with("code/")
        && page
            .parsed
            .frontmatter
            .provenance
            .iter()
            .any(frontmatter_value_has_code_source_span)
}

fn frontmatter_value_has_code_source_span(value: &Value) -> bool {
    let Value::Array(sources) = value else {
        return false;
    };
    sources.iter().any(frontmatter_source_has_code_span)
}

fn frontmatter_source_has_code_span(value: &Value) -> bool {
    let Value::Object(source) = value else {
        return false;
    };
    let Some(file) = source
        .get(gobby_core::codewiki_contract::PROVENANCE_FILE_KEY)
        .and_then(Value::as_str)
    else {
        return false;
    };
    let Some(Value::Array(ranges)) =
        source.get(gobby_core::codewiki_contract::PROVENANCE_RANGES_KEY)
    else {
        return false;
    };
    is_code_source_path(file) && ranges.iter().any(frontmatter_range_is_valid)
}

fn frontmatter_range_is_valid(value: &Value) -> bool {
    if let Some(range) = value.as_str() {
        is_line_span(range)
    } else {
        value.as_u64().is_some_and(|line| line > 0)
    }
}

fn supported_claim_lines(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    claims: &[ClaimLine],
) -> BTreeSet<usize> {
    let page_path = page.relative_path.to_string_lossy().replace('\\', "/");
    let page_title = crate::lint::title_for_page(page);
    let page_slug = slugify(&page_title);
    let section_headings: BTreeSet<String> = provenance
        .links()
        .iter()
        .filter(|link| link.section.page_path.to_string_lossy().replace('\\', "/") == page_path)
        .filter(|link| {
            link.section.section_id == page_slug
                || page
                    .parsed
                    .headings
                    .iter()
                    .any(|heading| heading.title == link.section.heading)
        })
        .map(|link| link.section.heading.clone())
        .collect();

    if section_headings.is_empty() {
        return BTreeSet::new();
    }

    claims
        .iter()
        .filter_map(|claim| {
            claim
                .heading
                .as_ref()
                .is_some_and(|heading| section_headings.contains(heading))
                .then_some(claim.line)
        })
        .collect()
}

#[derive(Debug)]
struct ClaimLine {
    line: usize,
    heading: Option<String>,
    text: String,
    kind: ClaimKind,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ClaimKind {
    Prose,
    Structural,
}

/// Extract auditable prose claims from markdown body lines.
///
/// Frontmatter is skipped, headings update ignored-section state, empty lines
/// and markdown-only structural lines are ignored, and lines with inline
/// source support are treated as already supported.
fn claim_lines(page: &WikiPage, options: &AuditOptions) -> Vec<ClaimLine> {
    let mut claims = Vec::new();
    let mut offset = 0;
    let mut frontmatter_marker: Option<&str> = None;
    let mut fence: Option<MarkdownFence> = None;
    let mut current_heading: Option<String> = None;
    let mut in_comment = false;

    for raw_line in page.markdown.split_inclusive('\n') {
        let line = raw_line.trim_end_matches(['\r', '\n']);
        let trimmed = line.trim();
        let line_start = offset;
        offset += raw_line.len();

        if line_start == 0 && (trimmed == "---" || trimmed == "+++") {
            frontmatter_marker = Some(trimmed);
            continue;
        }
        if let Some(marker) = frontmatter_marker {
            if trimmed == marker {
                frontmatter_marker = None;
            }
            continue;
        }
        if let Some(active_fence) = fence {
            if markdown_fence_closes(line, active_fence) {
                fence = None;
            }
            continue;
        } else if let Some(opening_fence) = markdown_fence_start(line) {
            fence = Some(opening_fence);
            continue;
        }
        if in_comment {
            if trimmed.contains("-->") {
                in_comment = false;
            }
            continue;
        }
        if trimmed.contains("<!--") {
            if !trimmed.contains("-->") {
                in_comment = true;
            }
            continue;
        }
        if trimmed.is_empty() {
            continue;
        }
        if is_thematic_break(trimmed) {
            continue;
        }
        if let Some(heading) = heading_title(trimmed) {
            current_heading = Some(heading);
            continue;
        }
        if ignored_claim_section(current_heading.as_deref(), options) || ignored_claim_line(trimmed)
        {
            continue;
        }
        let text = trimmed
            .strip_prefix("- ")
            .or_else(|| trimmed.strip_prefix("* "))
            .or_else(|| trimmed.strip_prefix("+ "))
            .unwrap_or(trimmed)
            .trim();
        if text.is_empty() {
            continue;
        }
        claims.push(ClaimLine {
            line: line_number(&page.markdown, line_start),
            heading: current_heading.clone(),
            text: text.to_string(),
            kind: claim_kind(text),
        });
    }

    claims
}

fn claim_kind(text: &str) -> ClaimKind {
    if is_structural_codewiki_claim(text) {
        ClaimKind::Structural
    } else {
        ClaimKind::Prose
    }
}

fn is_structural_codewiki_claim(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    lower.starts_with("module:")
        || lower.starts_with("parent:")
        || lower.starts_with("signature:")
        || lower.starts_with("source path:")
        || lower.starts_with("component:")
        || lower.starts_with("components:")
        || lower.starts_with("[[code/")
}

fn heading_title(line: &str) -> Option<String> {
    parse_atx_heading(line)
        .map(|(_, heading)| heading)
        .filter(|heading| !heading.is_empty())
}

fn ignored_claim_section(heading: Option<&str>, options: &AuditOptions) -> bool {
    heading.is_some_and(|heading| options.ignores_section(heading))
}

fn ignored_claim_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.starts_with("sources:")
        || lower.starts_with("source path:")
        || lower.starts_with("citation:")
        || lower.starts_with("citations:")
        || lower == "- none recorded."
}

fn is_thematic_break(line: &str) -> bool {
    let compact = line
        .chars()
        .filter(|ch| !ch.is_whitespace())
        .collect::<String>();
    if compact.len() < 3 {
        return false;
    }
    let Some(marker @ ('-' | '*' | '_')) = compact.chars().next() else {
        return false;
    };
    compact.chars().all(|ch| ch == marker)
}

fn has_inline_source_support(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("[[knowledge/sources/")
        || lower.contains("(knowledge/sources/")
        || has_code_source_span(line)
        || has_link_like_source_token(&lower, "citation:")
        || has_link_like_source_token(&lower, "source:")
        || lower.contains("gwiki-source:")
}

fn has_code_source_span(line: &str) -> bool {
    let mut rest = line;
    while let Some(open_index) = rest.find('[') {
        let after_open = &rest[open_index + 1..];
        let Some(close_index) = after_open.find(']') else {
            return false;
        };
        if is_code_source_span(&after_open[..close_index]) {
            return true;
        }
        rest = &after_open[close_index + 1..];
    }
    false
}

fn is_code_source_span(candidate: &str) -> bool {
    let Some((path, span)) = candidate.rsplit_once(':') else {
        return false;
    };
    is_code_source_path(path) && is_line_span(span)
}

fn is_code_source_path(path: &str) -> bool {
    !path.is_empty()
        && !path.contains(char::is_whitespace)
        && (path.contains('/') || path.contains('.'))
        && !path.contains("://")
}

fn is_line_span(span: &str) -> bool {
    let Some((start, end)) = span.split_once('-') else {
        return span.parse::<usize>().is_ok_and(|line| line > 0);
    };
    let Ok(start) = start.parse::<usize>() else {
        return false;
    };
    let Ok(end) = end.parse::<usize>() else {
        return false;
    };
    start > 0 && end >= start
}

fn has_link_like_source_token(line: &str, token: &str) -> bool {
    let mut start = 0;
    while let Some(relative_index) = line[start..].find(token) {
        let index = start + relative_index;
        let before = line[..index].chars().next_back();
        let has_boundary =
            before.is_none_or(|ch| !(ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-')));
        let after = line[index + token.len()..].trim_start();
        if has_boundary
            && (after.starts_with("http://")
                || after.starts_with("https://")
                || after.starts_with("[[knowledge/sources/")
                || after.starts_with('[')
                || after.starts_with("(knowledge/sources/")
                || after.starts_with("knowledge/sources/")
                || after.starts_with("gwiki-source:"))
        {
            return true;
        }
        start = index + token.len();
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
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
            .find(|claim| claim.path == PathBuf::from("knowledge/topics/claims.md"))
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
        std::fs::create_dir_all(topic_page.parent().expect("topic parent"))
            .expect("create topic dir");
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
}
