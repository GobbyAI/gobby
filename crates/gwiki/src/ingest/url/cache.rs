use std::fs;
use std::path::Path;

use crate::WikiError;
use crate::paths::raw_source_path;
use crate::sources::{
    FetchProvenance, SourceKind, SourceManifest, SourceRecord, canonicalize_location,
};
use crate::support::time::parse_unix_ms;

use super::CachedUrlIngest;

const MILLIS_PER_HOUR: u64 = 3_600_000;

pub(super) fn cached_url(
    vault_root: &Path,
    manifest: &SourceManifest,
    requested_url: &str,
    now: &str,
    max_age_hours: u64,
) -> Result<Option<CachedUrlIngest>, WikiError> {
    if max_age_hours == 0 {
        return Ok(None);
    }

    let canonical_location = canonicalize_location(requested_url);
    let Some(record) = newest_fetched_record(manifest, &canonical_location) else {
        return Ok(None);
    };
    let Some(now_ms) = parse_unix_ms(now) else {
        return Ok(None);
    };
    let Some(verified_ms) = parse_unix_ms(&record.last_verified_at) else {
        return Ok(None);
    };
    let Some(age_ms) = now_ms.checked_sub(verified_ms) else {
        return Ok(None);
    };
    if age_ms > max_age_hours.saturating_mul(MILLIS_PER_HOUR)
        || !cache_artifacts_exist(vault_root, record)
    {
        return Ok(None);
    }

    Ok(Some(CachedUrlIngest {
        requested_url: requested_url.to_string(),
        source_id: record.id.clone(),
        fetched_at: record.fetched_at.clone(),
        age_hours: age_ms / MILLIS_PER_HOUR,
    }))
}

fn newest_fetched_record<'a>(
    manifest: &'a SourceManifest,
    canonical_location: &str,
) -> Option<&'a SourceRecord> {
    manifest
        .entries
        .iter()
        .filter(|record| {
            record.fetch_provenance == FetchProvenance::Fetched
                && record.canonical_location == canonical_location
        })
        .max_by_key(|record| parse_unix_ms(&record.last_verified_at).unwrap_or_default())
}

fn cache_artifacts_exist(vault_root: &Path, record: &SourceRecord) -> bool {
    let Ok(raw_path) = raw_source_path(&record.id) else {
        return false;
    };
    if !vault_root.join(raw_path).is_file() {
        return false;
    }
    record.kind == SourceKind::Url || source_asset_exists(vault_root, &record.id)
}

fn source_asset_exists(vault_root: &Path, source_id: &str) -> bool {
    let Ok(entries) = fs::read_dir(vault_root.join("raw/assets")) else {
        return false;
    };
    entries.filter_map(Result::ok).any(|entry| {
        entry.path().is_file()
            && entry
                .path()
                .file_stem()
                .is_some_and(|stem| stem == source_id)
    })
}
