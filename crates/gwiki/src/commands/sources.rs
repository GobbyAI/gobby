use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::Serialize;
use serde_json::{Value, json};

use crate::commands::index;
use crate::frontmatter;
use crate::sources::{CompileStatus, SourceKind, SourceManifest, SourceRecord};
use crate::support::counts::IndexCounts;
use crate::support::scope::{resolve_command_scope, resolved_scope_identity};
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(scope.root())?;
    let output_scope = resolved_scope_identity(&scope);
    let manifest = SourceManifest::read(scope.root())?;
    let mut degradations = Vec::new();
    let sources = source_entries(scope.root(), &manifest.entries, &mut degradations)?;
    Ok(render_sources(output_scope, sources, degradations))
}

pub(crate) fn execute_remove(
    id: String,
    selection: ScopeSelection,
    dry_run: bool,
    keep_asset: bool,
) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;
    ensure_scope_root(scope.root())?;
    let output_scope = resolved_scope_identity(&scope);
    let manifest = SourceManifest::read(scope.root())?;
    let record = manifest
        .entries
        .iter()
        .find(|entry| entry.id == id)
        .cloned()
        .ok_or_else(|| WikiError::NotFound {
            resource: "source",
            id: id.clone(),
        })?;

    let mut degradations = Vec::new();
    let source = source_entry(scope.root(), &record, &mut degradations)?;
    let raw_path = raw_source_path(&record.id)?;
    let asset_path = source
        .source_asset
        .as_deref()
        .map(|path| source_asset_path(scope.root(), path))
        .transpose()?;

    let mut path_changes = PathChanges::default();

    stage_raw_source(
        scope.root(),
        &raw_path,
        source.raw_exists,
        true,
        &mut path_changes,
    )?;

    if let Some(asset_path) = asset_path {
        stage_source_asset(
            scope.root(),
            &asset_path,
            true,
            keep_asset,
            &mut path_changes,
            &mut degradations,
        )?;
    }

    let index_status = if dry_run {
        IndexStatus::not_run()
    } else {
        match SourceManifest::remove(scope.root(), &record.id) {
            Ok(Some(removed)) => {
                if let Err(error) = remove_staged_source_files(scope.root(), &mut path_changes) {
                    rollback_removed_source(scope.root(), removed, &error)?;
                    return Err(error);
                }
            }
            Ok(None) => {
                degradations.push("manifest_entry_missing_after_plan".to_string());
                path_changes.removed_paths.clear();
            }
            Err(error) => {
                degradations.push(format!("manifest_remove_failed:{error}"));
                path_changes.removed_paths.clear();
            }
        }
        match index::index_resolved_scope(&scope) {
            Ok(counts) => IndexStatus::indexed(counts),
            Err(error) => {
                degradations.push(format!("index_failed:{error}"));
                IndexStatus::degraded()
            }
        }
    };

    Ok(render_remove_source(RemoveSourceRender {
        scope: output_scope,
        dry_run,
        source,
        removed_paths: path_changes.removed_paths,
        kept_paths: path_changes.kept_paths,
        missing_paths: path_changes.missing_paths,
        index_status,
        degradations,
        follow_up: follow_up_for_removed_source(&record),
    }))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct SourceListEntry {
    id: String,
    kind: SourceKind,
    title: Option<String>,
    location: String,
    citation: Option<String>,
    content_hash: String,
    fetched_at: String,
    compile_status: CompileStatus,
    raw_path: String,
    raw_exists: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    source_asset: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
struct IndexStatus {
    status: &'static str,
    index_required: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    indexed: Option<IndexedCounts>,
}

impl IndexStatus {
    fn not_run() -> Self {
        Self {
            status: "not_run",
            index_required: false,
            indexed: None,
        }
    }

    fn indexed(counts: IndexCounts) -> Self {
        Self {
            status: "indexed",
            index_required: false,
            indexed: Some(IndexedCounts::from(counts)),
        }
    }

    fn degraded() -> Self {
        Self {
            status: "degraded",
            index_required: true,
            indexed: None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
struct IndexedCounts {
    documents: usize,
    chunks: usize,
    links: usize,
    sources: usize,
    ingestions: usize,
}

impl From<IndexCounts> for IndexedCounts {
    fn from(counts: IndexCounts) -> Self {
        Self {
            documents: counts.documents,
            chunks: counts.chunks,
            links: counts.links,
            sources: counts.sources,
            ingestions: counts.ingestions,
        }
    }
}

struct RemoveSourceRender {
    scope: ScopeIdentity,
    dry_run: bool,
    source: SourceListEntry,
    removed_paths: Vec<String>,
    kept_paths: Vec<String>,
    missing_paths: Vec<String>,
    index_status: IndexStatus,
    degradations: Vec<String>,
    follow_up: Vec<&'static str>,
}

#[derive(Debug, Default)]
struct PathChanges {
    removed_paths: Vec<String>,
    kept_paths: Vec<String>,
    missing_paths: Vec<String>,
}

fn source_entries(
    vault_root: &Path,
    records: &[SourceRecord],
    degradations: &mut Vec<String>,
) -> Result<Vec<SourceListEntry>, WikiError> {
    records
        .iter()
        .map(|record| source_entry(vault_root, record, degradations))
        .collect()
}

fn source_entry(
    vault_root: &Path,
    record: &SourceRecord,
    degradations: &mut Vec<String>,
) -> Result<SourceListEntry, WikiError> {
    let raw_path = raw_source_path(&record.id)?;
    let raw_full_path = vault_root.join(&raw_path);
    let raw_exists = raw_full_path.is_file();
    let source_asset = if raw_exists {
        read_source_asset(&raw_full_path, &raw_path, degradations)?
    } else {
        degradations.push(format!("raw_missing:{}", display_path(&raw_path)));
        None
    };

    Ok(SourceListEntry {
        id: record.id.clone(),
        kind: record.kind.clone(),
        title: record.title.clone(),
        location: record.location.clone(),
        citation: record.citation.clone(),
        content_hash: record.content_hash.clone(),
        fetched_at: record.fetched_at.clone(),
        compile_status: record.compile_status.clone(),
        raw_path: display_path(&raw_path),
        raw_exists,
        source_asset,
    })
}

fn read_source_asset(
    raw_full_path: &Path,
    raw_path: &Path,
    degradations: &mut Vec<String>,
) -> Result<Option<String>, WikiError> {
    let markdown = fs::read_to_string(raw_full_path).map_err(|error| WikiError::Io {
        action: "read raw source markdown",
        path: Some(raw_full_path.to_path_buf()),
        source: error,
    })?;
    let frontmatter = match frontmatter::parse_frontmatter(&markdown) {
        Ok(parsed) => parsed,
        Err(error) => {
            degradations.push(format!(
                "frontmatter_parse_failed:{}:{error}",
                display_path(raw_path)
            ));
            return Ok(None);
        }
    };

    match frontmatter.metadata.unknown.get("source_asset") {
        None => Ok(None),
        Some(Value::String(path)) => {
            let path = path.trim();
            if path.is_empty() {
                Ok(None)
            } else {
                Ok(Some(path.to_string()))
            }
        }
        Some(_) => {
            degradations.push(format!(
                "source_asset_invalid_type:{}",
                display_path(raw_path)
            ));
            Ok(None)
        }
    }
}

fn stage_raw_source(
    vault_root: &Path,
    raw_path: &Path,
    raw_exists: bool,
    dry_run: bool,
    path_changes: &mut PathChanges,
) -> Result<(), WikiError> {
    if raw_exists {
        path_changes.removed_paths.push(display_path(raw_path));
        if !dry_run {
            fs::remove_file(vault_root.join(raw_path)).map_err(|error| WikiError::Io {
                action: "remove raw source markdown",
                path: Some(vault_root.join(raw_path)),
                source: error,
            })?;
        }
    } else {
        path_changes.missing_paths.push(display_path(raw_path));
    }
    Ok(())
}

fn stage_source_asset(
    vault_root: &Path,
    asset_path: &Path,
    dry_run: bool,
    keep_asset: bool,
    path_changes: &mut PathChanges,
    degradations: &mut Vec<String>,
) -> Result<(), WikiError> {
    if keep_asset {
        path_changes.kept_paths.push(display_path(asset_path));
        return Ok(());
    }

    let full_path = vault_root.join(asset_path);
    if full_path.is_file() {
        path_changes.removed_paths.push(display_path(asset_path));
        if !dry_run {
            fs::remove_file(&full_path).map_err(|error| WikiError::Io {
                action: "remove raw source asset",
                path: Some(full_path),
                source: error,
            })?;
        }
        return Ok(());
    }

    path_changes.missing_paths.push(display_path(asset_path));
    degradations.push(format!("source_asset_missing:{}", display_path(asset_path)));
    Ok(())
}

fn remove_staged_source_files(
    vault_root: &Path,
    path_changes: &mut PathChanges,
) -> Result<(), WikiError> {
    let planned = std::mem::take(&mut path_changes.removed_paths);
    let mut removed = Vec::with_capacity(planned.len());
    for relative in planned {
        let full_path = vault_root.join(&relative);
        match fs::remove_file(&full_path) {
            Ok(()) => removed.push(relative),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                path_changes.missing_paths.push(relative);
            }
            Err(source) => {
                path_changes.removed_paths = removed;
                return Err(WikiError::Io {
                    action: "remove source file",
                    path: Some(full_path),
                    source,
                });
            }
        }
    }
    path_changes.removed_paths = removed;
    Ok(())
}

fn rollback_removed_source(
    vault_root: &Path,
    record: SourceRecord,
    original_error: &WikiError,
) -> Result<(), WikiError> {
    SourceManifest::update(vault_root, |manifest| {
        if manifest.entries.iter().any(|entry| entry.id == record.id) {
            return Ok(false);
        }
        manifest.entries.push(record);
        Ok(true)
    })
    .map_err(|rollback_error| WikiError::Config {
        detail: format!(
            "failed to roll back source manifest after source file removal failed: {rollback_error}; original error: {original_error}"
        ),
    })
}

fn raw_source_path(id: &str) -> Result<PathBuf, WikiError> {
    if id.trim().is_empty()
        || id.contains('/')
        || id.contains('\\')
        || Path::new(id)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(WikiError::InvalidInput {
            field: "source_id",
            message: format!("unsafe source id `{id}`"),
        });
    }
    Ok(Path::new("raw").join(format!("{id}.md")))
}

fn source_asset_path(vault_root: &Path, value: &str) -> Result<PathBuf, WikiError> {
    let path = safe_vault_relative_path("source_asset", value)?;
    if !is_raw_asset_path(&path) {
        return Err(WikiError::InvalidInput {
            field: "source_asset",
            message: format!(
                "source_asset `{}` must be under raw/assets",
                display_path(&path)
            ),
        });
    }

    let full_path = vault_root.join(&path);
    if full_path.exists() {
        let vault = vault_root.canonicalize().map_err(|error| WikiError::Io {
            action: "resolve wiki vault root",
            path: Some(vault_root.to_path_buf()),
            source: error,
        })?;
        let target = full_path.canonicalize().map_err(|error| WikiError::Io {
            action: "resolve raw source asset",
            path: Some(full_path.clone()),
            source: error,
        })?;
        if !target.starts_with(&vault) {
            return Err(WikiError::InvalidInput {
                field: "source_asset",
                message: format!("source_asset `{}` resolves outside the vault", value.trim()),
            });
        }
    }

    Ok(path)
}

fn safe_vault_relative_path(field: &'static str, value: &str) -> Result<PathBuf, WikiError> {
    let value = value.trim();
    if value.is_empty() || value.contains('\\') {
        return Err(WikiError::InvalidInput {
            field,
            message: "path must be a non-empty vault-relative path".to_string(),
        });
    }

    let path = Path::new(value);
    if path.is_absolute() {
        return Err(WikiError::InvalidInput {
            field,
            message: format!("absolute path `{value}` is not allowed"),
        });
    }

    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => normalized.push(part),
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(WikiError::InvalidInput {
                    field,
                    message: format!("path `{value}` must stay inside the vault"),
                });
            }
        }
    }

    if normalized.as_os_str().is_empty() {
        return Err(WikiError::InvalidInput {
            field,
            message: "path must include a file name".to_string(),
        });
    }

    Ok(normalized)
}

fn is_raw_asset_path(path: &Path) -> bool {
    let mut components = path.components();
    matches!(components.next(), Some(Component::Normal(value)) if value == "raw")
        && matches!(components.next(), Some(Component::Normal(value)) if value == "assets")
        && components.next().is_some()
}

fn ensure_scope_root(root: &Path) -> Result<(), WikiError> {
    if root.is_dir() {
        return Ok(());
    }
    Err(WikiError::InvalidScope {
        detail: format!(
            "wiki scope root is missing or not a directory: {}",
            root.display()
        ),
    })
}

fn follow_up_for_removed_source(record: &SourceRecord) -> Vec<&'static str> {
    if record.compile_status == CompileStatus::Compiled {
        vec!["audit_recommended"]
    } else {
        Vec::new()
    }
}

fn render_sources(
    scope: ScopeIdentity,
    sources: Vec<SourceListEntry>,
    degradations: Vec<String>,
) -> CommandOutcome {
    let payload = json!({
        "command": "sources",
        "scope": scope,
        "status": "ok",
        "sources": sources,
        "degradations": degradations,
    });
    let text = format!(
        "Sources
Scope: {scope}
Count: {}
Degradations: {}",
        payload["sources"].as_array().map_or(0, Vec::len),
        payload["degradations"].as_array().map_or(0, Vec::len)
    );
    super::scoped_outcome("sources", &scope, payload, text)
}

fn render_remove_source(render: RemoveSourceRender) -> CommandOutcome {
    let status = if render.dry_run {
        "would_remove"
    } else {
        "removed"
    };
    let scope = render.scope.clone();
    let payload = json!({
        "command": "remove-source",
        "scope": scope,
        "status": status,
        "dry_run": render.dry_run,
        "source": render.source,
        "removed_paths": render.removed_paths,
        "kept_paths": render.kept_paths,
        "missing_paths": render.missing_paths,
        "index_status": render.index_status,
        "degradations": render.degradations,
        "follow_up": render.follow_up,
    });
    let text = format!(
        "Source removal {}
Scope: {}
Source: {}
Removed paths: {}
Kept paths: {}
Missing paths: {}",
        if render.dry_run {
            "preview"
        } else {
            "complete"
        },
        payload["scope"],
        payload["source"]["id"].as_str().unwrap_or("<unknown>"),
        payload["removed_paths"].as_array().map_or(0, Vec::len),
        payload["kept_paths"].as_array().map_or(0, Vec::len),
        payload["missing_paths"].as_array().map_or(0, Vec::len)
    );
    super::scoped_outcome("remove-source", &scope, payload, text)
}

fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind};

    #[test]
    fn source_listing_reads_manifest_raw_path_and_asset() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_manifest_source(temp.path(), CompileStatus::Pending);
        write_raw_source(
            temp.path(),
            &record.id,
            "source_asset: raw/assets/source.pdf\n",
        );

        let mut degradations = Vec::new();
        let entries = source_entries(
            temp.path(),
            std::slice::from_ref(&record),
            &mut degradations,
        )
        .expect("source entries");

        assert!(degradations.is_empty(), "{degradations:?}");
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].id, record.id);
        assert_eq!(entries[0].raw_path, format!("raw/{}.md", record.id));
        assert!(entries[0].raw_exists);
        assert_eq!(
            entries[0].source_asset.as_deref(),
            Some("raw/assets/source.pdf")
        );
    }

    #[test]
    fn missing_raw_file_degrades_without_failing_source_listing() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_manifest_source(temp.path(), CompileStatus::Pending);

        let mut degradations = Vec::new();
        let entries = source_entries(
            temp.path(),
            std::slice::from_ref(&record),
            &mut degradations,
        )
        .expect("source entries");

        assert_eq!(entries[0].raw_path, format!("raw/{}.md", record.id));
        assert!(!entries[0].raw_exists);
        assert_eq!(
            degradations,
            vec![format!("raw_missing:raw/{}.md", record.id)]
        );
    }

    #[test]
    fn source_asset_path_rejects_traversal_absolute_and_non_raw_assets() {
        let temp = tempfile::tempdir().expect("tempdir");

        for value in ["../escape.pdf", "/tmp/escape.pdf", "wiki/topics/article.md"] {
            let error = source_asset_path(temp.path(), value).expect_err("unsafe path rejected");
            assert_eq!(error.code(), "invalid_input");
        }
    }

    #[test]
    fn manifest_remove_deletes_only_matching_source_record() {
        let temp = tempfile::tempdir().expect("tempdir");
        let first = seed_manifest_source(temp.path(), CompileStatus::Pending);
        let second = SourceManifest::register(
            temp.path(),
            SourceDraft {
                location: "https://example.com/second".to_string(),
                kind: SourceKind::Url,
                fetched_at: "2026-05-30T00:00:00Z".to_string(),
                content: b"second source".to_vec(),
                title: Some("Second source".to_string()),
                citation: None,
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register second source");

        let removed = SourceManifest::remove(temp.path(), &first.id)
            .expect("remove source")
            .expect("source removed");
        let manifest = SourceManifest::read(temp.path()).expect("read manifest");

        assert_eq!(removed.id, first.id);
        assert_eq!(manifest.entries, vec![second]);
    }

    #[test]
    fn compiled_source_requests_audit_follow_up() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = seed_manifest_source(temp.path(), CompileStatus::Compiled);

        assert_eq!(
            follow_up_for_removed_source(&record),
            vec!["audit_recommended"]
        );
    }

    fn seed_manifest_source(root: &Path, compile_status: CompileStatus) -> SourceRecord {
        SourceManifest::register(
            root,
            SourceDraft {
                location: "https://example.com/source".to_string(),
                kind: SourceKind::Url,
                fetched_at: "2026-05-30T00:00:00Z".to_string(),
                content: b"source body".to_vec(),
                title: Some("Example source".to_string()),
                citation: Some("Example citation".to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status,
            },
        )
        .expect("register source")
    }

    fn write_raw_source(root: &Path, id: &str, extra_frontmatter: &str) {
        let raw_path = root.join("raw").join(format!("{id}.md"));
        fs::create_dir_all(raw_path.parent().expect("raw parent")).expect("create raw dir");
        fs::write(
            raw_path,
            format!(
                "---
source_kind: url
{extra_frontmatter}---

# Example source
"
            ),
        )
        .expect("write raw source");
    }
}
