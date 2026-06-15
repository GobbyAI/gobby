use std::path::{Path, PathBuf};

use ::postgres::Transaction;

use super::{
    StoreError, WikiChunk, WikiDocumentKind, WikiIngestionEvent, WikiLink, WikiStoreScope,
};

pub(super) const MAX_ID_LEN: usize = 63;
pub(super) const HASH_SUFFIX_LEN: usize = 12;

pub(super) fn display_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

pub(super) fn validate_chunk_paths(path: &Path, chunks: &[WikiChunk]) -> Result<(), StoreError> {
    for chunk in chunks {
        validate_matching_path("chunk.path", path, &chunk.path)?;
    }
    Ok(())
}

pub(super) fn validate_link_paths(path: &Path, links: &[WikiLink]) -> Result<(), StoreError> {
    for link in links {
        validate_matching_path("link.path", path, &link.path)?;
    }
    Ok(())
}

pub(super) fn validate_matching_path(
    field: &'static str,
    expected: &Path,
    found: &Path,
) -> Result<(), StoreError> {
    if equivalent_display_path(expected, found) {
        return Ok(());
    }
    Err(StoreError::InvalidData {
        field,
        message: format!(
            "expected {}, found {}",
            display_path(expected),
            display_path(found)
        ),
    })
}

pub(super) fn equivalent_display_path(left: &Path, right: &Path) -> bool {
    display_path(left) == display_path(right)
}

pub(super) fn platform_path_from_display(path: &str) -> PathBuf {
    if std::path::MAIN_SEPARATOR == '/' {
        PathBuf::from(path)
    } else {
        PathBuf::from(path.replace('/', std::path::MAIN_SEPARATOR_STR))
    }
}

pub(super) fn scoped_id(
    prefix: &str,
    scope: &WikiStoreScope,
    path: &Path,
    suffix: Option<&str>,
) -> String {
    match suffix {
        Some(value) => scoped_text_id(prefix, scope, path, &[value]),
        None => scoped_text_id(prefix, scope, path, &[]),
    }
}

pub(super) fn scoped_text_id(
    prefix: &str,
    scope: &WikiStoreScope,
    path: &Path,
    suffixes: &[&str],
) -> String {
    let mut id = format!(
        "{prefix}:{}:{}:{}",
        scope.scope_kind(),
        scope.scope_id(),
        display_path(path)
    );
    for suffix in suffixes {
        id.push(':');
        id.push_str(suffix);
    }
    cap_scoped_id(id)
}

fn cap_scoped_id(id: String) -> String {
    let hash = gobby_core::indexing::content_hash(id.as_bytes());
    cap_scoped_id_with_hash(id, &hash)
}

pub(super) fn cap_scoped_id_with_hash(id: String, hash: &str) -> String {
    if id.len() <= MAX_ID_LEN {
        return id;
    }

    let suffix = if hash.len() >= HASH_SUFFIX_LEN {
        &hash[..HASH_SUFFIX_LEN]
    } else {
        hash
    };
    let prefix_len = MAX_ID_LEN.saturating_sub(suffix.len()).saturating_sub(1);
    let mut prefix = String::new();
    for ch in id.chars() {
        if prefix.len() + ch.len_utf8() > prefix_len {
            break;
        }
        prefix.push(ch);
    }
    format!("{prefix}-{suffix}")
}

pub(super) fn document_kind_name(kind: WikiDocumentKind) -> &'static str {
    match kind {
        WikiDocumentKind::SourceCatalog => "source_catalog",
        WikiDocumentKind::SourceNote => "source_note",
        WikiDocumentKind::Concept => "concept",
        WikiDocumentKind::Topic => "topic",
        WikiDocumentKind::CodeDoc => "code_doc",
    }
}

pub(super) fn ingestion_status(event: WikiIngestionEvent) -> &'static str {
    match event {
        WikiIngestionEvent::Added => "added",
        WikiIngestionEvent::Changed => "changed",
        WikiIngestionEvent::Deleted => "deleted",
        WikiIngestionEvent::Unchanged => "unchanged",
        WikiIngestionEvent::Skipped => "skipped",
    }
}

pub(crate) fn link_kind(target: &str) -> &'static str {
    let trimmed = target.trim();
    if trimmed.starts_with("//") || trimmed.starts_with("\\\\") || has_uri_scheme(trimmed) {
        "markdown"
    } else {
        "wiki"
    }
}

fn has_uri_scheme(target: &str) -> bool {
    let Some((scheme, _rest)) = target.split_once(':') else {
        return false;
    };
    let mut chars = scheme.chars();
    chars.next().is_some_and(|ch| ch.is_ascii_alphabetic())
        && chars.all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '.' | '-'))
}

pub(super) fn rollback_link_replacement(tx: Transaction<'_>, path: &str) {
    if let Err(error) = tx.rollback() {
        log::error!("failed to rollback gwiki link replacement for {path}: {error}");
    }
}

pub(super) fn rollback_chunk_replacement(tx: Transaction<'_>, path: &str) {
    if let Err(error) = tx.rollback() {
        log::error!("failed to rollback gwiki chunk replacement for {path}: {error}");
    }
}

pub fn configured_memory_index_limit_bytes() -> Option<u64> {
    let env_key = super::MAX_MEMORY_INDEX_BYTES_ENV;
    match std::env::var(env_key) {
        Ok(raw) => raw
            .parse::<u64>()
            .ok()
            .filter(|value| *value > 0)
            .or_else(|| {
                eprintln!("warning: ignoring invalid {env_key}={raw}");
                None
            }),
        Err(_) => None,
    }
}
