//! Per-page `.json` agent export siblings (#17730).
//!
//! Every non-excluded vault page gets a structured metadata sibling under
//! `outputs/pages/` mirroring the vault tree (`knowledge/concepts/gobby.md`
//! → `outputs/pages/knowledge/concepts/gobby.json`): frontmatter, outbound
//! links, lifecycle, composed page confidence (#17728), and audit claim
//! classifications (#17729). Recomputed from the vault on every export —
//! nothing here is a source of truth.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use super::write::write_export_batch;
use super::{ExportArtifact, ExportKind, ExportRequest};
use crate::WikiError;
use crate::audit::{AuditOptions, ClassifiedClaim, classify_pages};
use crate::health::{build_source_needle_index, load_provenance, page_confidence_by_path};
use crate::lint::{WikiPage, collect_pages};
use crate::sources::SourceManifest;

/// Directory under `outputs/` holding the mirrored per-page `.json` siblings.
const PAGES_EXPORT_DIR: &str = "pages";

pub fn export_agent_pages(root: &Path) -> Result<Vec<ExportArtifact>, WikiError> {
    let pages: Vec<WikiPage> = collect_pages(root)?
        .into_iter()
        .filter(|page| !crate::lifecycle::excluded_from_default_surfaces(&page.parsed.frontmatter))
        .collect();
    let manifest = SourceManifest::read(root)?;
    let provenance = load_provenance(root)?;
    let needle_index = build_source_needle_index(&manifest.entries);
    let confidence = page_confidence_by_path(&pages, &manifest.entries, &provenance, &needle_index);
    let classifications = classify_pages(root, &pages, &AuditOptions::from_env())?;
    let claims_by_page = group_claims_by_page(&classifications.claims);

    let mut requests = Vec::with_capacity(pages.len());
    for page in &pages {
        let filename = sibling_filename(&page.relative_path)?;
        let contents = render_page_json(page, &confidence, &claims_by_page)?;
        requests.push(ExportRequest {
            filename,
            kind: ExportKind::Page,
            contents,
        });
    }

    let artifacts = write_export_batch(root, requests)?;
    let written: BTreeSet<PathBuf> = artifacts
        .iter()
        .map(|artifact| artifact.path.clone())
        .collect();
    prune_stale_page_exports(&root.join("outputs").join(PAGES_EXPORT_DIR), &written)?;
    Ok(artifacts)
}

fn sibling_filename(relative_path: &Path) -> Result<String, WikiError> {
    let sibling = relative_path.with_extension("json");
    let normalized = sibling.to_string_lossy().replace('\\', "/");
    if normalized.is_empty() {
        return Err(WikiError::InvalidInput {
            field: "page",
            message: format!(
                "page path produced an empty export filename: {}",
                relative_path.display()
            ),
        });
    }
    Ok(format!("{PAGES_EXPORT_DIR}/{normalized}"))
}

fn render_page_json(
    page: &WikiPage,
    confidence: &BTreeMap<PathBuf, u8>,
    claims_by_page: &BTreeMap<&Path, Vec<&ClassifiedClaim>>,
) -> Result<String, WikiError> {
    let frontmatter = &page.parsed.frontmatter;
    let mut object = Map::new();
    object.insert(
        "path".to_string(),
        json!(page.relative_path.to_string_lossy().replace('\\', "/")),
    );
    object.insert("frontmatter".to_string(), frontmatter.as_json());
    object.insert(
        "lifecycle".to_string(),
        frontmatter
            .lifecycle
            .map_or(Value::Null, |lifecycle| json!(lifecycle.as_str())),
    );
    let outbound_links: BTreeSet<&str> = page
        .parsed
        .links
        .iter()
        .map(|link| link.target.as_str())
        .collect();
    object.insert("outbound_links".to_string(), json!(outbound_links));
    object.insert(
        "confidence".to_string(),
        confidence
            .get(&page.relative_path)
            .map_or(Value::Null, |score| json!(score)),
    );
    object.insert(
        "audit_claims".to_string(),
        audit_claims_json(claims_by_page.get(page.relative_path.as_path())),
    );

    let mut contents =
        serde_json::to_string_pretty(&Value::Object(object)).map_err(|error| WikiError::Json {
            action: "serialize page export",
            path: Some(page.relative_path.clone()),
            source: error,
        })?;
    contents.push('\n');
    Ok(contents)
}

fn group_claims_by_page(claims: &[ClassifiedClaim]) -> BTreeMap<&Path, Vec<&ClassifiedClaim>> {
    let mut grouped: BTreeMap<&Path, Vec<&ClassifiedClaim>> = BTreeMap::new();
    for claim in claims {
        grouped.entry(claim.path.as_path()).or_default().push(claim);
    }
    grouped
}

fn audit_claims_json(claims: Option<&Vec<&ClassifiedClaim>>) -> Value {
    use crate::audit::ClaimClassification;

    let claims = claims.map(Vec::as_slice).unwrap_or_default();
    let count = |classification: ClaimClassification| {
        claims
            .iter()
            .filter(|claim| claim.classification == classification)
            .count()
    };
    let entries: Vec<Value> = claims
        .iter()
        .map(|claim| {
            json!({
                "line": claim.line,
                "heading": claim.heading,
                "classification": claim.classification,
            })
        })
        .collect();
    json!({
        "extracted": count(ClaimClassification::Extracted),
        "inferred": count(ClaimClassification::Inferred),
        "ambiguous": count(ClaimClassification::Ambiguous),
        "claims": entries,
    })
}

/// Remove `.json` siblings under `outputs/pages/` whose vault page no longer
/// exists (or is newly excluded), then drop directories left empty. Keeps
/// re-exports deterministic without clearing the tree up front.
fn prune_stale_page_exports(
    pages_root: &Path,
    written: &BTreeSet<PathBuf>,
) -> Result<(), WikiError> {
    if !pages_root.exists() {
        return Ok(());
    }
    prune_directory(pages_root, written)?;
    Ok(())
}

/// Returns whether `directory` is empty after pruning, so parents can drop it.
fn prune_directory(directory: &Path, written: &BTreeSet<PathBuf>) -> Result<bool, WikiError> {
    let entries = fs::read_dir(directory).map_err(|error| WikiError::Io {
        action: "read page export directory",
        path: Some(directory.to_path_buf()),
        source: error,
    })?;
    let mut empty = true;
    for entry in entries {
        let entry = entry.map_err(|error| WikiError::Io {
            action: "read page export directory entry",
            path: Some(directory.to_path_buf()),
            source: error,
        })?;
        let path = entry.path();
        if path.is_dir() {
            if prune_directory(&path, written)? {
                let _ = fs::remove_dir(&path);
            } else {
                empty = false;
            }
            continue;
        }
        let stale = path
            .extension()
            .is_some_and(|extension| extension == "json")
            && !written.contains(&path);
        if stale {
            fs::remove_file(&path).map_err(|error| WikiError::Io {
                action: "remove stale page export",
                path: Some(path.clone()),
                source: error,
            })?;
        } else {
            empty = false;
        }
    }
    Ok(empty)
}
