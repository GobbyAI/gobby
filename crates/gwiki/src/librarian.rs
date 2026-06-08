use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::{Value, json};

use crate::frontmatter::parse_frontmatter;
use crate::provenance::ProvenanceGraph;
use crate::{ScopeIdentity, WikiError, audit, health, lint};

const LIBRARIAN_DIR: &str = "meta/librarian";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Options {
    pub shared_code_graph_available: bool,
    pub semantic_available: bool,
    pub model_available: bool,
}

impl Options {
    pub fn offline() -> Self {
        Self {
            shared_code_graph_available: false,
            semantic_available: false,
            model_available: false,
        }
    }
}

impl Default for Options {
    fn default() -> Self {
        Self::offline()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ProposalsReport {
    pub scope: ScopeIdentity,
    pub checks: Vec<CheckReport>,
    pub suggested_tasks: Vec<SuggestedTask>,
    pub suggested_patch_diffs: Vec<SuggestedPatchDiff>,
    pub artifacts: LibrarianArtifacts,
    pub dependency_classification: DependencyClassification,
}

impl ProposalsReport {
    #[cfg(test)]
    fn check(&self, name: &str) -> &CheckReport {
        self.checks
            .iter()
            .find(|check| check.name == name)
            .unwrap_or_else(|| panic!("missing check {name}"))
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct CheckReport {
    pub name: &'static str,
    pub available: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    pub items: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SuggestedTask {
    pub title: String,
    pub description: String,
    pub paths: Vec<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SuggestedPatchDiff {
    pub path: PathBuf,
    pub summary: String,
    pub diff: String,
    pub applies_to_canonical_content: bool,
    pub requires_acceptance: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LibrarianArtifacts {
    pub proposals_json: PathBuf,
    pub proposals_markdown: PathBuf,
    pub audit_annotations_json: PathBuf,
    pub stale_pages_json: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
pub struct DependencyClassification {
    pub hard: Vec<&'static str>,
    pub optional: Vec<&'static str>,
    pub multimodal: &'static str,
}

pub fn run(
    vault_root: &Path,
    scope: ScopeIdentity,
    options: Options,
) -> Result<ProposalsReport, WikiError> {
    let health_report = health::inspect(vault_root, scope.clone())?;
    let audit_report =
        audit::run_with_options(vault_root, scope.clone(), audit::AuditOptions::from_env())?;
    let lint_report = lint::run(vault_root, scope.clone())?;
    let pages = lint::collect_pages(vault_root)?;
    let provenance = ProvenanceGraph::load_from_vault(vault_root)?;

    let stale_pages = health_report.stale_pages.clone();
    let missing_citations = unique_paths(
        audit_report
            .unsupported_claims
            .iter()
            .map(|claim| claim.path.clone()),
    );
    let broken_links = unique_paths(
        lint_report
            .broken_links
            .iter()
            .map(|issue| issue.path.clone()),
    );
    let weak_provenance = weak_provenance_pages(&pages, &provenance);
    let outdated_codewiki = outdated_codewiki_pages(&pages);

    let mut checks = vec![
        available_check("stale_pages", stale_pages.clone()),
        available_check("missing_citations", missing_citations.clone()),
        available_check("broken_links", broken_links.clone()),
        available_check("weak_provenance", weak_provenance.clone()),
    ];
    checks.push(optional_check(
        "outdated_codewiki",
        !outdated_codewiki.is_empty() || options.shared_code_graph_available,
        "shared code graph is unavailable; skipped outdated codewiki detection",
        outdated_codewiki.clone(),
    ));
    checks.push(optional_check(
        "semantic_gaps",
        options.semantic_available,
        "Qdrant or embeddings are unavailable; skipped semantic gap detection",
        Vec::new(),
    ));
    checks.push(optional_check(
        "patch_suggestions",
        options.model_available,
        "model provider is unavailable; emitted deterministic task proposals only",
        Vec::new(),
    ));

    let suggested_tasks = suggested_tasks(
        &health_report.uncited_sources,
        &stale_pages,
        &missing_citations,
        &broken_links,
        &weak_provenance,
        &outdated_codewiki,
    );
    let suggested_patch_diffs = suggested_patch_diffs(&stale_pages, &missing_citations);
    let artifacts = artifacts();
    let report = ProposalsReport {
        scope,
        checks,
        suggested_tasks,
        suggested_patch_diffs,
        artifacts,
        dependency_classification: DependencyClassification {
            hard: vec!["PostgreSQL index", "vault"],
            optional: vec![
                "FalkorDB/shared code graph",
                "Qdrant+embeddings",
                "model provider",
            ],
            multimodal: "none; transcription, vision, and video providers are not used",
        },
    };

    persist_report(vault_root, &report)?;
    Ok(report)
}

pub fn render_text(report: &ProposalsReport) -> String {
    let mut text = format!("Librarian proposals\nScope: {}\n", report.scope);
    for check in &report.checks {
        let status = if check.available {
            "available"
        } else {
            "unavailable"
        };
        text.push_str(&format!(
            "\n## {} ({status})\n{} item(s)\n",
            check.name,
            check.items.len()
        ));
        if let Some(note) = &check.note {
            text.push_str(note);
            text.push('\n');
        }
        for path in &check.items {
            text.push_str("- ");
            text.push_str(&path.display().to_string());
            text.push('\n');
        }
    }
    text.push_str("\n## Suggested tasks\n");
    for task in &report.suggested_tasks {
        text.push_str("- ");
        text.push_str(&task.title);
        text.push('\n');
    }
    text
}

fn available_check(name: &'static str, items: Vec<PathBuf>) -> CheckReport {
    CheckReport {
        name,
        available: true,
        note: None,
        items,
    }
}

fn optional_check(
    name: &'static str,
    available: bool,
    unavailable_note: &'static str,
    items: Vec<PathBuf>,
) -> CheckReport {
    CheckReport {
        name,
        available,
        note: (!available).then(|| unavailable_note.to_string()),
        items: if available { items } else { Vec::new() },
    }
}

fn weak_provenance_pages(pages: &[lint::WikiPage], provenance: &ProvenanceGraph) -> Vec<PathBuf> {
    pages
        .iter()
        .filter(|page| page_is_codewiki(page))
        .filter(|page| !provenance_mentions_page(provenance, &page.relative_path))
        .map(|page| page.relative_path.clone())
        .collect()
}

fn provenance_mentions_page(provenance: &ProvenanceGraph, path: &Path) -> bool {
    let path = path.to_string_lossy();
    provenance
        .links()
        .iter()
        .any(|link| link.section.page_path.to_string_lossy() == path)
}

fn outdated_codewiki_pages(pages: &[lint::WikiPage]) -> Vec<PathBuf> {
    pages
        .iter()
        .filter(|page| page_is_codewiki(page))
        .filter(|page| frontmatter_flag(&page.markdown, "codewiki_status", "stale"))
        .map(|page| page.relative_path.clone())
        .collect()
}

fn page_is_codewiki(page: &lint::WikiPage) -> bool {
    page.parsed
        .frontmatter
        .generated_by
        .as_deref()
        .is_some_and(|generated_by| generated_by.contains("codewiki"))
}

fn frontmatter_flag(markdown: &str, key: &str, expected: &str) -> bool {
    parse_frontmatter(markdown)
        .ok()
        .and_then(|parsed| parsed.metadata.unknown.get(key).cloned())
        .and_then(|value| match value {
            Value::String(value) => Some(value == expected),
            Value::Bool(value) => Some(value && expected == "true"),
            _ => None,
        })
        .unwrap_or(false)
}

fn suggested_tasks(
    uncited_sources: &[health::HealthSourceIssue],
    stale_pages: &[PathBuf],
    missing_citations: &[PathBuf],
    broken_links: &[PathBuf],
    weak_provenance: &[PathBuf],
    outdated_codewiki: &[PathBuf],
) -> Vec<SuggestedTask> {
    let mut tasks = Vec::new();
    push_task(
        &mut tasks,
        !stale_pages.is_empty(),
        "Refresh stale wiki pages",
        "Review stale pages and refresh source support before accepting canonical edits.",
        stale_pages,
    );
    let source_ids = uncited_sources
        .iter()
        .map(|source| source.source_id.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    push_task(
        &mut tasks,
        !missing_citations.is_empty() || !source_ids.is_empty(),
        "Add missing citations for unsupported claims",
        &format!("Unsupported claims need citation review. Uncited sources: {source_ids}"),
        missing_citations,
    );
    push_task(
        &mut tasks,
        !broken_links.is_empty(),
        "Repair broken wiki links",
        "Broken links should be retargeted or removed after human review.",
        broken_links,
    );
    push_task(
        &mut tasks,
        !weak_provenance.is_empty(),
        "Strengthen weak provenance",
        "Attach source-to-section provenance before relying on these pages.",
        weak_provenance,
    );
    push_task(
        &mut tasks,
        !outdated_codewiki.is_empty(),
        "Refresh outdated codewiki pages",
        "Codewiki pages are stale; regenerate or accept a reviewed patch.",
        outdated_codewiki,
    );
    tasks
}

fn push_task(
    tasks: &mut Vec<SuggestedTask>,
    include: bool,
    title: &str,
    description: &str,
    paths: &[PathBuf],
) {
    if include {
        tasks.push(SuggestedTask {
            title: title.to_string(),
            description: description.to_string(),
            paths: paths.to_vec(),
        });
    }
}

fn suggested_patch_diffs(
    stale_pages: &[PathBuf],
    missing_citations: &[PathBuf],
) -> Vec<SuggestedPatchDiff> {
    unique_paths(stale_pages.iter().chain(missing_citations).cloned())
        .into_iter()
        .map(|path| SuggestedPatchDiff {
            path: path.clone(),
            summary: "Add citation refresh notes after human acceptance".to_string(),
            diff: format!(
                "--- a/{0}\n+++ b/{0}\n@@\n+<!-- librarian proposal: refresh citations and stale claims before accepting -->\n",
                path.display()
            ),
            applies_to_canonical_content: true,
            requires_acceptance: true,
        })
        .collect()
}

fn unique_paths(paths: impl Iterator<Item = PathBuf>) -> Vec<PathBuf> {
    paths.collect::<BTreeSet<_>>().into_iter().collect()
}

fn artifacts() -> LibrarianArtifacts {
    LibrarianArtifacts {
        proposals_json: PathBuf::from("meta/librarian/proposals.json"),
        proposals_markdown: PathBuf::from("meta/librarian/proposals.md"),
        audit_annotations_json: PathBuf::from("meta/librarian/audit-annotations.json"),
        stale_pages_json: PathBuf::from("meta/librarian/stale-pages.json"),
    }
}

fn persist_report(vault_root: &Path, report: &ProposalsReport) -> Result<(), WikiError> {
    let dir = vault_root.join(LIBRARIAN_DIR);
    std::fs::create_dir_all(&dir).map_err(|source| WikiError::Io {
        action: "create librarian metadata directory",
        path: Some(dir.clone()),
        source,
    })?;
    write_json(vault_root, &report.artifacts.proposals_json, report)?;
    write_text(
        vault_root,
        &report.artifacts.proposals_markdown,
        &render_text(report),
    )?;
    write_json(
        vault_root,
        &report.artifacts.audit_annotations_json,
        &json!({
            "missing_citations": report.checks.iter().find(|check| check.name == "missing_citations"),
            "weak_provenance": report.checks.iter().find(|check| check.name == "weak_provenance"),
        }),
    )?;
    write_json(
        vault_root,
        &report.artifacts.stale_pages_json,
        &json!({
            "stale_pages": report.checks.iter().find(|check| check.name == "stale_pages"),
            "outdated_codewiki": report.checks.iter().find(|check| check.name == "outdated_codewiki"),
        }),
    )
}

fn write_json<T: Serialize>(
    vault_root: &Path,
    relative: &Path,
    value: &T,
) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    let bytes = serde_json::to_vec_pretty(value).map_err(|source| WikiError::Json {
        action: "serialize librarian metadata",
        path: Some(path.clone()),
        source,
    })?;
    std::fs::write(&path, bytes).map_err(|source| WikiError::Io {
        action: "write librarian metadata",
        path: Some(path),
        source,
    })
}

fn write_text(vault_root: &Path, relative: &Path, text: &str) -> Result<(), WikiError> {
    let path = vault_root.join(relative);
    std::fs::write(&path, text).map_err(|source| WikiError::Io {
        action: "write librarian metadata",
        path: Some(path),
        source,
    })
}

#[cfg(test)]
mod tests {
    use std::path::{Path, PathBuf};

    use crate::ScopeIdentity;
    use crate::sources::{SourceDraft, SourceManifest};

    use super::*;

    #[test]
    fn librarian_detects_and_proposes_without_rewriting_pages() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let source = SourceManifest::register(
            root,
            SourceDraft::url(
                "https://example.com/uncited",
                "2026-05-29T12:00:00Z",
                "uncited source",
            )
            .with_citation("Uncited Example"),
        )
        .expect("source registered");
        write_page(
            root,
            "wiki/topics/stale.md",
            "---\ntitle: Stale\nstale: true\n---\n# Stale\nUnsupported operational claim.\nSee [[Missing]].\n",
        );
        write_page(
            root,
            "wiki/code/example.md",
            "---\ntitle: Example code\ngenerated_by: gcode-codewiki\nsource_spans:\n  - path: src/lib.rs\n    start_line: 1\n    end_line: 1\ncodewiki_status: stale\n---\n# Example code\nDocuments old code.\n",
        );

        let original_page =
            std::fs::read_to_string(root.join("wiki/topics/stale.md")).expect("read page");
        let report =
            run(root, ScopeIdentity::topic("ops"), Options::offline()).expect("librarian runs");

        assert_eq!(
            report.check("stale_pages").items,
            vec![PathBuf::from("wiki/topics/stale.md")]
        );
        assert_eq!(
            report.check("missing_citations").items,
            vec![PathBuf::from("wiki/topics/stale.md")]
        );
        assert_eq!(
            report.check("broken_links").items,
            vec![PathBuf::from("wiki/topics/stale.md")]
        );
        assert_eq!(
            report.check("weak_provenance").items,
            vec![PathBuf::from("wiki/code/example.md")]
        );
        assert_eq!(
            report.check("outdated_codewiki").items,
            vec![PathBuf::from("wiki/code/example.md")]
        );
        assert!(!report.check("patch_suggestions").available);
        assert!(
            report
                .suggested_tasks
                .iter()
                .any(|task| task.title.contains("Refresh stale wiki pages"))
        );
        assert!(
            report
                .suggested_patch_diffs
                .iter()
                .all(|diff| diff.applies_to_canonical_content)
        );
        assert_eq!(
            std::fs::read_to_string(root.join("wiki/topics/stale.md")).expect("read page"),
            original_page
        );
        assert!(root.join("meta/librarian/proposals.json").exists());
        assert!(root.join("meta/librarian/audit-annotations.json").exists());
        assert!(root.join("meta/librarian/stale-pages.json").exists());
        assert!(
            report
                .suggested_tasks
                .iter()
                .any(|task| task.description.contains(&source.id))
        );
    }

    #[test]
    fn librarian_degrades_each_optional_check_independently() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        write_page(
            root,
            "wiki/topics/page.md",
            "---\ntitle: Page\n---\n# Page\nSupported enough. [source](https://example.com)\n",
        );

        let report =
            run(root, ScopeIdentity::topic("ops"), Options::offline()).expect("librarian runs");

        assert!(report.check("stale_pages").available);
        assert!(report.check("missing_citations").available);
        assert!(report.check("broken_links").available);
        assert!(report.check("weak_provenance").available);
        assert!(!report.check("outdated_codewiki").available);
        assert_eq!(
            report.check("outdated_codewiki").note.as_deref(),
            Some("shared code graph is unavailable; skipped outdated codewiki detection")
        );
        assert!(!report.check("semantic_gaps").available);
        assert!(!report.check("patch_suggestions").available);
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }
}
