use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;

use crate::lint::{WikiPage, collect_pages, line_number};
use crate::provenance::ProvenanceGraph;
use crate::sources::SourceManifest;
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

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<AuditReport, WikiError> {
    run_with_options(vault_root, scope, AuditOptions::from_env())
}

pub fn run_with_options(
    vault_root: &Path,
    scope: ScopeIdentity,
    options: AuditOptions,
) -> Result<AuditReport, WikiError> {
    let pages = collect_pages(vault_root)?;
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
    let supported_lines = supported_claim_lines(page, provenance, options);
    claim_lines(page, options)
        .into_iter()
        .filter_map(|claim| {
            if supported_lines.contains(&claim.line) || has_inline_source_support(&claim.text) {
                return None;
            }
            Some(UnsupportedClaim {
                path: page.relative_path.clone(),
                line: claim.line,
                heading: claim.heading,
                claim: claim.text,
                reason: "claim has no source provenance or inline citation".to_string(),
                source_context: Arc::clone(source_context),
            })
        })
        .collect()
}

fn supported_claim_lines(
    page: &WikiPage,
    provenance: &ProvenanceGraph,
    options: &AuditOptions,
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

    claim_lines(page, options)
        .into_iter()
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
    let mut in_fence = false;
    let mut current_heading: Option<String> = None;

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
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
            continue;
        }
        if in_fence || trimmed.is_empty() || trimmed.starts_with("<!--") {
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
        });
    }

    claims
}

fn heading_title(line: &str) -> Option<String> {
    let level = line.bytes().take_while(|byte| *byte == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }
    let rest = line[level..].trim();
    (!rest.is_empty()).then(|| rest.trim_end_matches('#').trim().to_string())
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

fn has_inline_source_support(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("[[wiki/sources/")
        || lower.contains("(wiki/sources/")
        || lower.contains("citation:")
        || lower.contains("source:")
        || lower.contains("gwiki-source:")
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
        let page = root.join("wiki/topics/claims.md");
        std::fs::create_dir_all(page.parent().expect("page parent")).expect("create wiki dir");
        std::fs::write(
            &page,
            "---\ntitle: Claims\nsource_kind: topic\n---\n# Claims\nUnsupported operational claim.\n",
        )
        .expect("write page");

        let report = run(root, ScopeIdentity::topic("ops")).expect("audit runs");

        assert_eq!(report.unsupported_claims.len(), 1);
        let claim = &report.unsupported_claims[0];
        assert_eq!(claim.path, PathBuf::from("wiki/topics/claims.md"));
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
    fn reports_include_paths_and_scope() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let page = root.join("wiki/topics/path-scope.md");
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
            Some("wiki/topics/path-scope.md")
        );
    }

    #[test]
    fn frontmatter_closes_only_on_matching_document_start_delimiter() {
        let page = WikiPage {
            path: PathBuf::from("wiki/topics/frontmatter.md"),
            relative_path: PathBuf::from("wiki/topics/frontmatter.md"),
            markdown: "+++\ntitle = \"Frontmatter\"\n---\nstill_frontmatter = true\n+++\n# Body\nClaim after TOML frontmatter.\n---\nClaim after thematic break.\n".to_string(),
            parsed: crate::markdown::parse_markdown(
                "wiki/topics/frontmatter.md",
                "# Body\n",
                std::iter::empty::<&str>(),
            )
            .expect("parse markdown"),
            has_frontmatter: true,
        };

        let claims = claim_lines(&page, &AuditOptions::default());

        assert_eq!(claims.len(), 3);
        assert_eq!(claims[0].text, "Claim after TOML frontmatter.");
        assert_eq!(claims[1].text, "---");
        assert_eq!(claims[2].text, "Claim after thematic break.");
    }

    #[test]
    fn configured_ignored_sections_extend_defaults() {
        let page = WikiPage {
            path: PathBuf::from("wiki/topics/release.md"),
            relative_path: PathBuf::from("wiki/topics/release.md"),
            markdown: "# Release\nClaim needing support.\n## Notes\nIgnored internal note.\n## Sources\nIgnored source note.\n".to_string(),
            parsed: crate::markdown::parse_markdown(
                "wiki/topics/release.md",
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
