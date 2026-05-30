use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::lint::{collect_pages, title_for_page};
use crate::provenance::ProvenanceGraph;
use crate::sources::{CompileStatus, SourceManifest, SourceRecord};
use crate::{ScopeIdentity, WikiError};

const AVERAGE_GREGORIAN_YEAR_SECONDS: u64 = 31_556_952;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthReport {
    pub command: &'static str,
    pub scope: ScopeIdentity,
    pub root: PathBuf,
    pub stale_pages: Vec<PathBuf>,
    pub stale_citations: Vec<HealthSourceIssue>,
    pub uncited_sources: Vec<HealthSourceIssue>,
    pub broken_links: Vec<crate::lint::LinkIssue>,
    pub duplicate_concepts: Vec<DuplicateConcept>,
    pub uncompiled_sources: Vec<HealthSourceIssue>,
    pub json_path: PathBuf,
    pub text_path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HealthSourceIssue {
    pub source_id: String,
    pub path: Option<PathBuf>,
    pub location: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DuplicateConcept {
    pub title: String,
    pub paths: Vec<PathBuf>,
}

pub fn run(vault_root: &Path, scope: ScopeIdentity) -> Result<HealthReport, WikiError> {
    let lint_report = crate::lint::run(vault_root, scope.clone())?;
    let pages = collect_pages(vault_root)?;
    let manifest = SourceManifest::read(vault_root)?;
    let provenance = load_provenance(vault_root)?;
    let stale_pages = stale_pages(&pages);
    let stale_citations = manifest
        .entries
        .iter()
        .filter(|entry| source_citation_is_stale(entry))
        .map(source_issue)
        .collect();
    let uncited_sources = manifest
        .entries
        .iter()
        .filter(|entry| !source_is_cited(entry, &pages, &provenance))
        .map(source_issue)
        .collect();
    let duplicate_concepts = duplicate_concepts(&pages);
    let uncompiled_sources = manifest
        .entries
        .iter()
        .filter(|entry| entry.compile_status == CompileStatus::Pending)
        .map(source_issue)
        .collect();
    let report = HealthReport {
        command: "health",
        scope,
        root: vault_root.to_path_buf(),
        stale_pages,
        stale_citations,
        uncited_sources,
        broken_links: lint_report.broken_links,
        duplicate_concepts,
        uncompiled_sources,
        json_path: PathBuf::from("meta/health/latest.json"),
        text_path: PathBuf::from("meta/health/latest.md"),
    };
    persist_report(vault_root, &report)?;
    Ok(report)
}

pub fn render_text(report: &HealthReport) -> String {
    let mut text = format!("Wiki health report\nScope: {}\n", report.scope);
    render_paths(&mut text, "Stale pages", &report.stale_pages);
    render_sources(&mut text, "Stale citations", &report.stale_citations);
    render_sources(&mut text, "Uncited sources", &report.uncited_sources);
    text.push_str("\nBroken links:\n");
    if report.broken_links.is_empty() {
        text.push_str("- none\n");
    } else {
        for issue in &report.broken_links {
            text.push_str("- ");
            text.push_str(&issue.path.display().to_string());
            text.push(':');
            text.push_str(&issue.line.to_string());
            text.push_str(" -> ");
            text.push_str(&issue.target);
            text.push('\n');
        }
    }
    if report.duplicate_concepts.is_empty() {
        text.push_str("\nDuplicate concepts:\n- none\n");
    } else {
        text.push_str("\nDuplicate concepts:\n");
        for duplicate in &report.duplicate_concepts {
            text.push_str("- ");
            text.push_str(&duplicate.title);
            text.push_str(": ");
            text.push_str(
                &duplicate
                    .paths
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", "),
            );
            text.push('\n');
        }
    }
    render_sources(&mut text, "Uncompiled sources", &report.uncompiled_sources);
    text
}

fn persist_report(vault_root: &Path, report: &HealthReport) -> Result<(), WikiError> {
    let health_dir = vault_root.join("meta").join("health");
    fs::create_dir_all(&health_dir).map_err(|error| WikiError::Io {
        action: "create health report directory",
        path: Some(health_dir.clone()),
        source: error.to_string(),
    })?;
    let json_path = vault_root.join(&report.json_path);
    let text_path = vault_root.join(&report.text_path);
    let json = serde_json::to_string_pretty(report).map_err(|error| WikiError::Json {
        action: "serialize health report",
        path: Some(json_path.clone()),
        source: error.to_string(),
    })?;
    fs::write(&json_path, json).map_err(|error| WikiError::Io {
        action: "write health JSON report",
        path: Some(json_path),
        source: error.to_string(),
    })?;
    fs::write(&text_path, render_text(report)).map_err(|error| WikiError::Io {
        action: "write health text report",
        path: Some(text_path),
        source: error.to_string(),
    })
}

fn stale_pages(pages: &[crate::lint::WikiPage]) -> Vec<PathBuf> {
    let mut paths: Vec<PathBuf> = pages
        .iter()
        .filter(|page| page_is_stale(page))
        .map(|page| page.relative_path.clone())
        .collect();
    paths.sort();
    paths
}

fn page_is_stale(page: &crate::lint::WikiPage) -> bool {
    let frontmatter = &page.parsed.frontmatter;
    if frontmatter
        .unknown
        .get("stale")
        .and_then(serde_json::Value::as_bool)
        == Some(true)
    {
        return true;
    }
    for key in ["status", "review_status"] {
        if frontmatter
            .unknown
            .get(key)
            .and_then(serde_json::Value::as_str)
            .is_some_and(|value| value.eq_ignore_ascii_case("stale"))
        {
            return true;
        }
    }
    frontmatter
        .unknown
        .get("stale_after")
        .and_then(serde_json::Value::as_str)
        .is_some_and(|value| !value.trim().is_empty())
}

fn source_citation_is_stale(source: &SourceRecord) -> bool {
    source.citation.is_some()
        && fetched_year(&source.fetched_at)
            .is_some_and(|year| year + 1 < approximate_current_year())
}

fn fetched_year(value: &str) -> Option<u64> {
    let year = value.get(0..4)?;
    (year.chars().all(|ch| ch.is_ascii_digit()))
        .then(|| year.parse().ok())
        .flatten()
}

fn approximate_current_year() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| 1970 + duration.as_secs() / AVERAGE_GREGORIAN_YEAR_SECONDS)
        .unwrap_or(1970)
}

fn load_provenance(vault_root: &Path) -> Result<ProvenanceGraph, WikiError> {
    let path = vault_root.join("meta").join("provenance.json");
    if path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)
    } else {
        Ok(ProvenanceGraph::default())
    }
}

fn source_is_cited(
    source: &SourceRecord,
    pages: &[crate::lint::WikiPage],
    provenance: &ProvenanceGraph,
) -> bool {
    if provenance
        .links_for_source(&source.id)
        .into_iter()
        .next()
        .is_some()
    {
        return true;
    }
    pages.iter().any(|page| {
        let text = &page.markdown;
        text.contains(&source.id)
            || text.contains(&source.location)
            || text.contains(&source.canonical_location)
            || source
                .citation
                .as_ref()
                .is_some_and(|citation| text.contains(citation))
    })
}

fn source_issue(source: &SourceRecord) -> HealthSourceIssue {
    HealthSourceIssue {
        source_id: source.id.clone(),
        path: Some(PathBuf::from("raw").join(format!("{}.md", source.id))),
        location: source.location.clone(),
    }
}

fn duplicate_concepts(pages: &[crate::lint::WikiPage]) -> Vec<DuplicateConcept> {
    let mut by_title: BTreeMap<String, (String, Vec<PathBuf>)> = BTreeMap::new();
    for page in pages {
        if !page.relative_path.starts_with("wiki/concepts") {
            continue;
        }
        let title = title_for_page(page);
        by_title
            .entry(title.to_ascii_lowercase())
            .or_insert_with(|| (title, Vec::new()))
            .1
            .push(page.relative_path.clone());
    }
    by_title
        .into_values()
        .filter_map(|(title, mut paths)| {
            paths.sort();
            (paths.len() > 1).then_some(DuplicateConcept { title, paths })
        })
        .collect()
}

fn render_paths(text: &mut String, heading: &str, paths: &[PathBuf]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if paths.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for path in paths {
        text.push_str("- ");
        text.push_str(&path.display().to_string());
        text.push('\n');
    }
}

fn render_sources(text: &mut String, heading: &str, sources: &[HealthSourceIssue]) {
    text.push('\n');
    text.push_str(heading);
    text.push_str(":\n");
    if sources.is_empty() {
        text.push_str("- none\n");
        return;
    }
    for source in sources {
        text.push_str("- ");
        text.push_str(&source.source_id);
        text.push_str(" (");
        text.push_str(&source.location);
        text.push_str(")\n");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{SourceDraft, SourceManifest};

    #[test]
    fn health_checks_required_cases() {
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
            "---\ntitle: Stale\nstale: true\n---\n# Stale\nSee [[Missing]].\n",
        );
        write_page(
            root,
            "wiki/concepts/cache-a.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept A.\n",
        );
        write_page(
            root,
            "wiki/concepts/cache-b.md",
            "---\ntitle: Cache\nsource_kind: concept\n---\n# Cache\nConcept B.\n",
        );

        let report = run(root, ScopeIdentity::topic("ops")).expect("health runs");

        assert_eq!(
            report.stale_pages,
            vec![PathBuf::from("wiki/topics/stale.md")]
        );
        assert_eq!(report.uncited_sources[0].source_id, source.id);
        assert_eq!(report.broken_links[0].target, "Missing");
        assert_eq!(report.duplicate_concepts[0].title, "Cache");
        assert_eq!(report.uncompiled_sources[0].source_id, source.id);
        assert!(root.join("meta/health/latest.json").exists());
        assert!(root.join("meta/health/latest.md").exists());
    }

    fn write_page(root: &Path, relative: &str, markdown: &str) {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("page parent")).expect("create parent");
        std::fs::write(path, markdown).expect("write page");
    }
}
