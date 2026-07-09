mod claims;
mod render;

#[cfg(test)]
mod tests;

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Serialize;

use crate::lint::collect_pages;
use crate::provenance::ProvenanceGraph;
use crate::sources::SourceManifest;
use crate::support::scope::scope_includes_page;
use crate::{ScopeIdentity, WikiError};

pub use render::render_text;

const DEFAULT_IGNORED_SECTIONS: &[&str] = &[
    "citations",
    "citation",
    "sources",
    "source",
    "backlinks",
    "extracts",
    "source excerpts",
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
    pub claims: Vec<ClassifiedClaim>,
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

/// Provenance classification for every claim line the audit examines.
///
/// `Extracted` claims carry direct per-claim support (a provenance-graph
/// section link or an inline citation). `Inferred` claims have page-level
/// grounding without a per-claim citation (structural lines on codewiki pages
/// whose frontmatter records code source spans, or daemon-synthesis sections
/// attributed to sources). `Ambiguous` covers explicitly flagged uncertainty
/// (`Conflicting claims` / `Missing evidence` territory) and claims with no
/// discernible provenance — the latter are also reported as unsupported.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum ClaimClassification {
    Extracted,
    Inferred,
    Ambiguous,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ClassifiedClaim {
    pub path: PathBuf,
    pub line: usize,
    pub heading: Option<String>,
    pub claim: String,
    pub classification: ClaimClassification,
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
    let manifest = SourceManifest::read(vault_root)?;
    let manifest_hashes = manifest
        .entries
        .iter()
        .map(|entry| entry.content_hash.clone())
        .collect::<BTreeSet<_>>();
    let source_context = Arc::new(source_context(manifest));
    let provenance = load_provenance(vault_root)?;
    let mut classified_claims = Vec::new();
    let mut unsupported_claims = Vec::new();
    for page in &pages {
        let analysis = claims::analyze_claims(
            page,
            &provenance,
            &source_context,
            &manifest_hashes,
            &options,
        );
        classified_claims.extend(analysis.classified);
        unsupported_claims.extend(analysis.unsupported);
    }

    Ok(AuditReport {
        command: "audit",
        scope,
        root: vault_root.to_path_buf(),
        claims: classified_claims,
        unsupported_claims,
        source_context,
    })
}

fn source_context(manifest: SourceManifest) -> Vec<AuditSourceContext> {
    manifest
        .entries
        .into_iter()
        .map(|entry| AuditSourceContext {
            path: Some(PathBuf::from("raw").join(format!("{}.md", entry.id))),
            source_id: entry.id,
            citation: entry.citation,
            location: Some(entry.location),
        })
        .collect()
}

fn load_provenance(vault_root: &Path) -> Result<ProvenanceGraph, WikiError> {
    let path = vault_root.join("meta").join("provenance.json");
    if path.exists() {
        ProvenanceGraph::load_from_vault(vault_root)
    } else {
        Ok(ProvenanceGraph::default())
    }
}
