//! Source-selection helpers shared by the `compile` command and the `upkeep`
//! conductor: resolve manifest records from user-facing selectors and turn
//! them into accepted research notes backed by raw source files.

use std::collections::HashSet;
use std::path::Path;

use crate::sources::{SourceManifest, SourceRecord};
use crate::{WikiError, paths, session};

/// Resolve `selectors` against the manifest into accepted notes, deduplicating
/// repeated selections of the same source while preserving selector order.
pub(crate) fn resolve_source_notes(
    vault_root: &Path,
    manifest: &SourceManifest,
    selectors: &[String],
) -> Result<Vec<session::AcceptedResearchNote>, WikiError> {
    let mut selected = Vec::new();
    let mut seen = HashSet::new();
    for selector in selectors {
        let record = resolve_source_selector(manifest, selector)?;
        if seen.insert(record.id.clone()) {
            selected.push(accepted_note_from_source(vault_root, record)?);
        }
    }
    Ok(selected)
}

pub(crate) fn resolve_source_selector<'a>(
    manifest: &'a SourceManifest,
    selector: &str,
) -> Result<&'a SourceRecord, WikiError> {
    let selector = selector.trim();
    if let Some(record) = manifest.entries.iter().find(|entry| entry.id == selector) {
        return Ok(record);
    }

    let selector_path = Path::new(selector);
    for record in &manifest.entries {
        if paths::raw_source_path(&record.id)? == selector_path {
            return Ok(record);
        }
    }

    let matches = manifest
        .entries
        .iter()
        .filter(|entry| entry.location == selector || entry.canonical_location == selector)
        .collect::<Vec<_>>();
    match matches.as_slice() {
        [record] => Ok(record),
        [] => Err(WikiError::NotFound {
            resource: "source",
            id: selector.to_string(),
        }),
        _ => Err(WikiError::InvalidInput {
            field: "source",
            message: format!(
                "source selector `{selector}` matched multiple records; pass a source id"
            ),
        }),
    }
}

pub(crate) fn accepted_note_from_source(
    vault_root: &Path,
    record: &SourceRecord,
) -> Result<session::AcceptedResearchNote, WikiError> {
    let raw_path = paths::raw_source_path(&record.id)?;
    let absolute_path = vault_root.join(&raw_path);
    match absolute_path.try_exists() {
        Ok(true) => {}
        Ok(false) => {
            return Err(WikiError::NotFound {
                resource: "raw_source",
                id: raw_path.display().to_string(),
            });
        }
        Err(error) => {
            return Err(WikiError::Io {
                action: "check raw source",
                path: Some(absolute_path),
                source: error,
            });
        }
    }

    Ok(session::AcceptedResearchNote {
        title: record
            .title
            .clone()
            .unwrap_or_else(|| record.location.clone()),
        path: raw_path,
        code_citations: Vec::new(),
        degradation: None,
    })
}

/// Re-point a loaded checkpoint's accepted notes at the current source manifest.
///
/// A source that is re-fetched by `gwiki refresh` gets a new content hash and
/// therefore a new `src-<hash>-<slug>` raw path, orphaning the path stored in an
/// older research checkpoint and deleting the old raw file. Because the
/// URL-derived slug is stable across re-fetches, each stale note is re-pointed to
/// the manifest record that shares its slug. Notes whose source no longer exists
/// are dropped so that one unrelated re-fetch cannot hard-fail an entire topic
/// compile (#17702). Returns `true` when any note was re-pointed or dropped.
pub(crate) fn reconcile_accepted_notes_with_manifest(
    vault_root: &Path,
    notes: &mut Vec<session::AcceptedResearchNote>,
    manifest: &SourceManifest,
) -> bool {
    let mut changed = false;
    notes.retain_mut(|note| {
        if vault_root.join(&note.path).is_file() {
            return true;
        }
        if let Some(current_path) =
            current_raw_path_for_stale_note(vault_root, manifest, &note.path)
        {
            note.path = current_path;
            changed = true;
            return true;
        }
        eprintln!(
            "warning: dropping accepted research note whose raw source is missing from the vault: {}",
            note.path.display()
        );
        changed = true;
        false
    });
    changed
}

/// Resolve the current raw path for a stale note by matching the stable
/// URL-derived slug embedded in its `src-<hash>-<slug>` filename against the
/// manifest. Returns `None` when no current source shares the slug or the
/// re-pointed raw file is itself absent.
fn current_raw_path_for_stale_note(
    vault_root: &Path,
    manifest: &SourceManifest,
    stale_path: &Path,
) -> Option<std::path::PathBuf> {
    let stale_slug = stale_path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .and_then(source_id_slug)?;
    let matches = manifest
        .entries
        .iter()
        .filter(|record| source_id_slug(&record.id) == Some(stale_slug))
        .collect::<Vec<_>>();
    let record = match matches.as_slice() {
        [record] => *record,
        [] => return None,
        _ => {
            eprintln!(
                "warning: stale accepted note `{}` matched multiple current source records with slug `{stale_slug}`; pass an explicit source id",
                stale_path.display()
            );
            return None;
        }
    };
    let current_path = paths::raw_source_path(&record.id).ok()?;
    vault_root
        .join(&current_path)
        .is_file()
        .then_some(current_path)
}

/// Extract the stable URL-derived slug from a `src-<hash>-<slug>` source id.
/// Returns `None` for hash-only ids (`src-<hash>`), which carry no slug.
fn source_id_slug(id: &str) -> Option<&str> {
    id.strip_prefix("src-")?
        .split_once('-')
        .map(|(_hash, slug)| slug)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceKind};
    use std::fs;
    use std::path::PathBuf;

    fn source_record(
        id: &str,
        location: &str,
        canonical_location: &str,
        title: Option<&str>,
    ) -> SourceRecord {
        SourceRecord {
            id: id.to_string(),
            location: location.to_string(),
            canonical_location: canonical_location.to_string(),
            kind: SourceKind::Markdown,
            fetched_at: "2026-06-14T00:00:00Z".to_string(),
            content_hash: format!("{id}-hash"),
            title: title.map(str::to_string),
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
            replay: None,
        }
    }

    fn write_raw_source(root: &Path, record: &SourceRecord) {
        let path = root.join(paths::raw_source_path(&record.id).expect("raw path"));
        fs::create_dir_all(path.parent().expect("raw parent")).expect("create raw parent");
        fs::write(&path, format!("# {}\n", record.id)).expect("write raw source");
    }

    #[test]
    fn source_selectors_resolve_id_raw_path_location_and_canonical_location() {
        let temp = tempfile::tempdir().expect("tempdir");
        let records = vec![
            source_record(
                "src-alpha",
                "alpha.md",
                "file:///vault/alpha.md",
                Some("Alpha"),
            ),
            source_record("src-beta", "beta.md", "file:///vault/beta.md", Some("Beta")),
            source_record(
                "src-gamma",
                "gamma.md",
                "file:///vault/gamma.md",
                Some("Gamma"),
            ),
            source_record("src-delta", "delta.md", "canonical:delta", None),
        ];
        for record in &records {
            write_raw_source(temp.path(), record);
        }
        let manifest = SourceManifest { entries: records };

        let notes = resolve_source_notes(
            temp.path(),
            &manifest,
            &[
                "src-alpha".to_string(),
                "raw/src-beta.md".to_string(),
                "gamma.md".to_string(),
                "canonical:delta".to_string(),
            ],
        )
        .expect("source notes");

        assert_eq!(
            notes
                .iter()
                .map(|note| note.path.clone())
                .collect::<Vec<_>>(),
            vec![
                PathBuf::from("raw/src-alpha.md"),
                PathBuf::from("raw/src-beta.md"),
                PathBuf::from("raw/src-gamma.md"),
                PathBuf::from("raw/src-delta.md"),
            ]
        );
        assert_eq!(
            notes
                .iter()
                .map(|note| note.title.as_str())
                .collect::<Vec<_>>(),
            vec!["Alpha", "Beta", "Gamma", "delta.md"]
        );
    }

    #[test]
    fn source_selection_dedupes_by_source_id_in_selector_order() {
        let temp = tempfile::tempdir().expect("tempdir");
        let alpha = source_record("src-alpha", "alpha.md", "canonical:alpha", Some("Alpha"));
        let beta = source_record("src-beta", "beta.md", "canonical:beta", Some("Beta"));
        write_raw_source(temp.path(), &alpha);
        write_raw_source(temp.path(), &beta);
        let manifest = SourceManifest {
            entries: vec![alpha, beta],
        };

        let notes = resolve_source_notes(
            temp.path(),
            &manifest,
            &[
                "src-beta".to_string(),
                "src-alpha".to_string(),
                "raw/src-beta.md".to_string(),
                "alpha.md".to_string(),
            ],
        )
        .expect("source notes");

        assert_eq!(
            notes
                .iter()
                .map(|note| note.path.clone())
                .collect::<Vec<_>>(),
            vec![
                PathBuf::from("raw/src-beta.md"),
                PathBuf::from("raw/src-alpha.md"),
            ]
        );
    }

    #[test]
    fn missing_source_selector_reports_source_not_found() {
        let manifest = SourceManifest {
            entries: vec![source_record(
                "src-alpha",
                "alpha.md",
                "canonical:alpha",
                Some("Alpha"),
            )],
        };
        let error = resolve_source_selector(&manifest, "missing").expect_err("missing source");

        match error {
            WikiError::NotFound { resource, id } => {
                assert_eq!(resource, "source");
                assert_eq!(id, "missing");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn ambiguous_non_id_selector_reports_invalid_input() {
        let manifest = SourceManifest {
            entries: vec![
                source_record("src-alpha", "shared.md", "canonical:alpha", Some("Alpha")),
                source_record("src-beta", "shared.md", "canonical:beta", Some("Beta")),
            ],
        };
        let error = resolve_source_selector(&manifest, "shared.md").expect_err("ambiguous source");

        match error {
            WikiError::InvalidInput { field, message } => {
                assert_eq!(field, "source");
                assert_eq!(
                    message,
                    "source selector `shared.md` matched multiple records; pass a source id"
                );
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn missing_raw_file_for_selected_source_reports_raw_source_not_found() {
        let temp = tempfile::tempdir().expect("tempdir");
        let manifest = SourceManifest {
            entries: vec![source_record(
                "src-alpha",
                "alpha.md",
                "canonical:alpha",
                Some("Alpha"),
            )],
        };

        let error = resolve_source_notes(temp.path(), &manifest, &["src-alpha".to_string()])
            .expect_err("missing raw source");

        match error {
            WikiError::NotFound { resource, id } => {
                assert_eq!(resource, "raw_source");
                assert_eq!(id, "raw/src-alpha.md");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }

    #[test]
    fn reconcile_repoints_stale_note_to_refetched_source() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        // Source re-fetched by refresh: new content hash, same URL slug.
        let current = source_record(
            "src-f8564a331c2de6-example-com-a",
            "https://example.com/a",
            "https://example.com/a",
            Some("Example A"),
        );
        write_raw_source(root, &current);
        let manifest = SourceManifest {
            entries: vec![current],
        };
        // Checkpoint still points at the pre-refresh raw path (file now absent).
        let mut notes = vec![session::AcceptedResearchNote {
            title: "Example A".to_string(),
            path: PathBuf::from("raw/src-ae5a51a7122bac-example-com-a.md"),
            code_citations: Vec::new(),
            degradation: None,
        }];

        let changed = reconcile_accepted_notes_with_manifest(root, &mut notes, &manifest);

        assert!(changed);
        assert_eq!(notes.len(), 1);
        assert_eq!(
            notes[0].path,
            PathBuf::from("raw/src-f8564a331c2de6-example-com-a.md")
        );
    }

    #[test]
    fn reconcile_drops_note_when_source_removed_from_manifest() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let manifest = SourceManifest {
            entries: Vec::new(),
        };
        let mut notes = vec![session::AcceptedResearchNote {
            title: "Gone".to_string(),
            path: PathBuf::from("raw/src-deadbeefdeadbe-example-com-gone.md"),
            code_citations: Vec::new(),
            degradation: None,
        }];

        let changed = reconcile_accepted_notes_with_manifest(root, &mut notes, &manifest);

        assert!(changed);
        assert!(notes.is_empty());
    }

    #[test]
    fn reconcile_drops_stale_note_when_slug_matches_multiple_current_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let first = source_record(
            "src-f8564a331c2de6-example-com-a",
            "https://example.com/a?one",
            "https://example.com/a?one",
            Some("Example A One"),
        );
        let second = source_record(
            "src-abcdefabcdef-example-com-a",
            "https://example.com/a?two",
            "https://example.com/a?two",
            Some("Example A Two"),
        );
        write_raw_source(root, &first);
        write_raw_source(root, &second);
        let manifest = SourceManifest {
            entries: vec![first, second],
        };
        let mut notes = vec![session::AcceptedResearchNote {
            title: "Example A".to_string(),
            path: PathBuf::from("raw/src-ae5a51a7122bac-example-com-a.md"),
            code_citations: Vec::new(),
            degradation: None,
        }];

        let changed = reconcile_accepted_notes_with_manifest(root, &mut notes, &manifest);

        assert!(changed);
        assert!(
            notes.is_empty(),
            "ambiguous stale slug should not pick an arbitrary current record"
        );
    }

    #[test]
    fn reconcile_leaves_present_notes_untouched() {
        let temp = tempfile::tempdir().expect("tempdir");
        let root = temp.path();
        let present = source_record(
            "src-livehash000000-example-com-b",
            "https://example.com/b",
            "https://example.com/b",
            Some("Example B"),
        );
        write_raw_source(root, &present);
        let present_path = paths::raw_source_path(&present.id).expect("raw path");
        let manifest = SourceManifest {
            entries: vec![present],
        };
        let mut notes = vec![session::AcceptedResearchNote {
            title: "Example B".to_string(),
            path: present_path.clone(),
            code_citations: Vec::new(),
            degradation: None,
        }];

        let changed = reconcile_accepted_notes_with_manifest(root, &mut notes, &manifest);

        assert!(!changed);
        assert_eq!(notes[0].path, present_path);
    }

    #[test]
    fn source_id_slug_extracts_stable_slug_across_hash_changes() {
        assert_eq!(
            source_id_slug("src-ae5a51a7122bac-https-example-com-a"),
            Some("https-example-com-a")
        );
        assert_eq!(
            source_id_slug("src-f8564a331c2de6-https-example-com-a"),
            Some("https-example-com-a")
        );
        assert_eq!(source_id_slug("src-0000000000000000"), None);
        assert_eq!(source_id_slug("knowledge/sources/foo"), None);
    }
}
