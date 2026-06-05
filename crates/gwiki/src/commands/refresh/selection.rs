use super::*;

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
