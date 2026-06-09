use super::*;
use crate::code_graph::AffectedPage;

pub(crate) fn select_sources(entries: &[SourceRecord], source_ids: &[String]) -> Selection {
    if source_ids.is_empty() {
        let mut planned = Vec::new();
        let mut skipped = Vec::new();
        let mut failed = Vec::new();
        for record in entries {
            match replay_kind(record) {
                Ok(_) => match RefreshPlan::from_record(record) {
                    Ok(plan) => planned.push(plan),
                    Err(error) => failed.push(refresh_plan_failure(record, error)),
                },
                Err(SelectionFailure::MissingReplayMetadata) => {
                    failed.push(selection_failure(
                        record,
                        SelectionFailure::MissingReplayMetadata,
                    ));
                }
                Err(SelectionFailure::UnsupportedSourceKind) => {
                    skipped.push(SkippedRefresh {
                        id: record.id.clone(),
                        location: record.location.clone(),
                        source_kind: record.kind.clone(),
                        code: "unsupported_source_kind".to_string(),
                        message: format!(
                            "source `{}` has kind `{}` and does not have a refresh replay contract",
                            record.id, record.kind
                        ),
                    });
                }
            }
        }
        return Selection {
            planned,
            skipped,
            failed,
        };
    }

    let mut seen = BTreeSet::new();
    let mut planned = Vec::new();
    let mut failed = Vec::new();
    for id in source_ids {
        if !seen.insert(id.clone()) {
            continue;
        }
        let Some(record) = entries.iter().find(|entry| entry.id == *id) else {
            failed.push(RefreshFailure {
                id: id.clone(),
                location: None,
                source_kind: None,
                code: "not_found".to_string(),
                message: format!("source `{id}` was not found"),
            });
            continue;
        };
        match replay_kind(record) {
            Ok(_) => match RefreshPlan::from_record(record) {
                Ok(plan) => planned.push(plan),
                Err(error) => failed.push(refresh_plan_failure(record, error)),
            },
            Err(error) => {
                failed.push(selection_failure(record, error));
            }
        }
    }

    Selection {
        planned,
        skipped: Vec::new(),
        failed,
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ChangeTriggeredSelection {
    pub(crate) source_ids_to_refresh: Vec<String>,
    pub(crate) pages_to_mark_stale: Vec<PathBuf>,
}

pub(crate) fn select_change_triggered_refresh(
    entries: &[SourceRecord],
    affected_pages: &[AffectedPage],
) -> ChangeTriggeredSelection {
    let mut source_ids_to_refresh = BTreeSet::new();
    let mut pages_to_mark_stale = BTreeSet::new();

    for page in affected_pages {
        let page_refreshes = page
            .source_ids
            .iter()
            .filter_map(|source_id| entries.iter().find(|entry| entry.id == *source_id))
            .filter(|record| is_markdown_replay(record))
            .map(|record| record.id.clone())
            .collect::<Vec<_>>();

        if page_refreshes.is_empty() {
            pages_to_mark_stale.insert(page.page_path.clone());
        } else {
            source_ids_to_refresh.extend(page_refreshes);
        }
    }

    ChangeTriggeredSelection {
        source_ids_to_refresh: source_ids_to_refresh.into_iter().collect(),
        pages_to_mark_stale: pages_to_mark_stale.into_iter().collect(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReplayKind {
    Url,
    LocalFile,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum SelectionFailure {
    MissingReplayMetadata,
    UnsupportedSourceKind,
}

pub(crate) fn replay_kind(record: &SourceRecord) -> Result<ReplayKind, SelectionFailure> {
    if is_url_source(record) {
        return Ok(ReplayKind::Url);
    }
    if local_file_replay(record).is_some() {
        return Ok(ReplayKind::LocalFile);
    }
    if is_local_file_source_kind(&record.kind) {
        Err(SelectionFailure::MissingReplayMetadata)
    } else {
        Err(SelectionFailure::UnsupportedSourceKind)
    }
}

pub(crate) fn replay_kind_name(record: &SourceRecord) -> &'static str {
    match replay_kind(record) {
        Ok(ReplayKind::Url) => "url",
        Ok(ReplayKind::LocalFile) => "local_file",
        Err(_) => "unsupported",
    }
}

pub(crate) fn local_file_replay(record: &SourceRecord) -> Option<(&Path, &SourceReplayOptions)> {
    match record.replay.as_ref()? {
        SourceReplay::LocalFile { path, options } => Some((path.as_path(), options)),
    }
}

fn is_markdown_replay(record: &SourceRecord) -> bool {
    let Some((path, _options)) = local_file_replay(record) else {
        return false;
    };
    record.kind == SourceKind::Markdown
        || path
            .extension()
            .and_then(|extension| extension.to_str())
            .is_some_and(|extension| {
                matches!(
                    extension.to_ascii_lowercase().as_str(),
                    "md" | "mdown" | "markdown"
                )
            })
}

fn is_local_file_source_kind(kind: &SourceKind) -> bool {
    matches!(
        kind,
        SourceKind::Audio
            | SourceKind::Image
            | SourceKind::Video
            | SourceKind::Pdf
            | SourceKind::Office
            | SourceKind::Html
            | SourceKind::Markdown
            | SourceKind::Text
            | SourceKind::File
    )
}

pub(crate) fn selection_failure(record: &SourceRecord, error: SelectionFailure) -> RefreshFailure {
    match error {
        SelectionFailure::MissingReplayMetadata => RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: "missing_replay_metadata".to_string(),
            message: format!(
                "source `{}` has kind `{}` but no local replay metadata",
                record.id, record.kind
            ),
        },
        SelectionFailure::UnsupportedSourceKind => RefreshFailure {
            id: record.id.clone(),
            location: Some(record.location.clone()),
            source_kind: Some(record.kind.clone()),
            code: "unsupported_source_kind".to_string(),
            message: format!(
                "source `{}` has kind `{}` and does not have a refresh replay contract",
                record.id, record.kind
            ),
        },
    }
}

fn refresh_plan_failure(record: &SourceRecord, error: WikiError) -> RefreshFailure {
    RefreshFailure {
        id: record.id.clone(),
        location: Some(record.location.clone()),
        source_kind: Some(record.kind.clone()),
        code: "invalid_source_id".to_string(),
        message: error.to_string(),
    }
}

fn is_url_source(record: &SourceRecord) -> bool {
    is_http_url(&record.location) || is_http_url(&record.canonical_location)
}

pub(crate) fn refresh_url(record: &SourceRecord) -> &str {
    if is_http_url(&record.location) {
        &record.location
    } else {
        &record.canonical_location
    }
}

fn is_http_url(value: &str) -> bool {
    let Ok(url) = url::Url::parse(value.trim()) else {
        return false;
    };
    matches!(url.scheme(), "http" | "https") && url.host_str().is_some_and(|host| !host.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::code_graph::AffectedPage;
    use crate::sources::{CompileStatus, IngestionMethod};

    #[test]
    fn change_triggered_refresh_selects_markdown_replay_and_stales_canonical_pages() {
        let refreshable = SourceRecord {
            id: "source-derived".to_string(),
            location: "notes/canonical.md".to_string(),
            canonical_location: "notes/canonical.md".to_string(),
            kind: SourceKind::Markdown,
            fetched_at: "2026-06-08T00:00:00Z".to_string(),
            content_hash: "hash".to_string(),
            title: None,
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Compiled,
            replay: Some(SourceReplay::LocalFile {
                path: PathBuf::from("notes/canonical.md"),
                options: SourceReplayOptions {
                    no_ai: false,
                    translate: false,
                    target_lang: None,
                    video_frame_interval_seconds: None,
                    transcription_routing: None,
                    vision_routing: None,
                    text_routing: None,
                },
            }),
        };
        let affected = vec![
            AffectedPage {
                page_path: PathBuf::from("code/derived.md"),
                source_ids: vec!["source-derived".to_string()],
                source_paths: vec![PathBuf::from("src/lib.rs")],
            },
            AffectedPage {
                page_path: PathBuf::from("code/canonical.md"),
                source_ids: vec!["source-canonical".to_string()],
                source_paths: vec![PathBuf::from("src/canonical.rs")],
            },
        ];

        let selection = select_change_triggered_refresh(&[refreshable], &affected);

        assert_eq!(selection.source_ids_to_refresh, vec!["source-derived"]);
        assert_eq!(
            selection.pages_to_mark_stale,
            vec![PathBuf::from("code/canonical.md")]
        );
    }
}
