use std::fs::{self, File, OpenOptions};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

use crate::WikiError;

use super::atomic::write_atomic;
use super::render::{
    canonicalize_location, existing_index_without_manifest, render_entry, source_id,
};
use super::types::{
    CompileStatus, IngestionMethod, SourceDraft, SourceDraftRef, SourceKind, SourceRecord,
};
use super::{
    DEFAULT_SOURCE_MANIFEST_LOCK_TIMEOUT, GENERATED_SOURCE_MANIFEST_END,
    GENERATED_SOURCE_MANIFEST_START, SOURCE_MANIFEST_LOCK_RETRY_DELAY,
    SOURCE_MANIFEST_LOCK_TIMEOUT_ENV, SOURCE_MARKER,
};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SourceManifest {
    pub entries: Vec<SourceRecord>,
}

impl SourceManifest {
    pub fn read(vault_root: &Path) -> Result<Self, WikiError> {
        let index_path = Self::index_path(vault_root);
        if !index_path.exists() {
            return Ok(Self::default());
        }

        let index = fs::read_to_string(&index_path).map_err(|error| WikiError::Io {
            action: "read raw source index",
            path: Some(index_path.clone()),
            source: error,
        })?;

        let mut entries = Vec::new();
        for line in index.lines() {
            let Some(marker_start) = line.find(SOURCE_MARKER) else {
                continue;
            };
            let json_start = marker_start + SOURCE_MARKER.len();
            let Some(marker_end) = line[json_start..].rfind("-->") else {
                return Err(WikiError::Json {
                    action: "parse raw source index marker",
                    path: Some(index_path),
                    source: serde_json::Error::io(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        "unterminated gwiki-source marker",
                    )),
                });
            };
            let json = line[json_start..json_start + marker_end].trim();
            let record = serde_json::from_str(json).map_err(|error| WikiError::Json {
                action: "parse raw source index marker",
                path: Some(index_path.clone()),
                source: error,
            })?;
            entries.push(record);
        }

        Ok(Self { entries })
    }

    pub fn register(vault_root: &Path, draft: SourceDraft) -> Result<SourceRecord, WikiError> {
        let content_hash = gobby_core::indexing::content_hash(&draft.content);
        Self::register_with_content_hash(vault_root, draft, content_hash)
    }

    pub(crate) fn register_borrowed(
        vault_root: &Path,
        draft: SourceDraftRef<'_>,
    ) -> Result<SourceRecord, WikiError> {
        let content_hash = gobby_core::indexing::content_hash(draft.content);
        Self::register_parts_with_content_hash(
            vault_root,
            SourceRecordParts {
                location: draft.location,
                kind: draft.kind,
                fetched_at: draft.fetched_at,
                title: draft.title,
                citation: draft.citation,
                license: draft.license,
                ingestion_method: draft.ingestion_method,
                compile_status: draft.compile_status,
            },
            content_hash,
        )
    }

    pub fn register_with_content_hash(
        vault_root: &Path,
        draft: SourceDraft,
        content_hash: String,
    ) -> Result<SourceRecord, WikiError> {
        Self::register_parts_with_content_hash(
            vault_root,
            SourceRecordParts {
                location: draft.location,
                kind: draft.kind,
                fetched_at: draft.fetched_at,
                title: draft.title,
                citation: draft.citation,
                license: draft.license,
                ingestion_method: draft.ingestion_method,
                compile_status: draft.compile_status,
            },
            content_hash,
        )
    }

    fn register_parts_with_content_hash(
        vault_root: &Path,
        draft: SourceRecordParts,
        content_hash: String,
    ) -> Result<SourceRecord, WikiError> {
        with_manifest_lock(vault_root, || {
            let mut manifest = Self::read(vault_root)?;
            let canonical_location = canonicalize_location(&draft.location);
            if let Some(existing) = manifest.entries.iter().find(|entry| {
                entry.canonical_location == canonical_location && entry.content_hash == content_hash
            }) {
                return Ok(existing.clone());
            }

            let record = SourceRecord {
                id: source_id(&canonical_location, &content_hash),
                location: draft.location,
                canonical_location,
                kind: draft.kind,
                fetched_at: draft.fetched_at,
                content_hash,
                title: draft.title,
                citation: draft.citation,
                license: draft.license,
                ingestion_method: draft.ingestion_method,
                compile_status: draft.compile_status,
                replay: None,
            };
            manifest.entries.push(record.clone());
            manifest.write_unlocked(vault_root)?;
            Ok(record)
        })
    }

    pub fn write(&self, vault_root: &Path) -> Result<(), WikiError> {
        with_manifest_lock(vault_root, || self.write_unlocked(vault_root))
    }

    fn write_unlocked(&self, vault_root: &Path) -> Result<(), WikiError> {
        let raw_dir = vault_root.join("raw");
        fs::create_dir_all(&raw_dir).map_err(|error| WikiError::Io {
            action: "create raw source directory",
            path: Some(raw_dir.clone()),
            source: error,
        })?;

        let index_path = Self::index_path(vault_root);
        let preserved = existing_index_without_manifest(&index_path)?;
        let mut index = preserved.prefix;
        if !self.entries.is_empty() {
            index.push_str(GENERATED_SOURCE_MANIFEST_START);
            index.push_str("\n## Source manifest\n\n");
        }
        for entry in &self.entries {
            render_entry(entry, &mut index)?;
        }
        if !self.entries.is_empty() {
            index.push_str(GENERATED_SOURCE_MANIFEST_END);
            index.push_str("\n\n");
        }
        if !preserved.suffix.is_empty() {
            index.push_str(&preserved.suffix);
            if !index.ends_with('\n') {
                index.push('\n');
            }
        }

        write_atomic(&index_path, index.as_bytes(), "write raw source index")
    }

    pub fn remove(vault_root: &Path, id: &str) -> Result<Option<SourceRecord>, WikiError> {
        with_manifest_lock(vault_root, || {
            let mut manifest = Self::read(vault_root)?;
            let Some(index) = manifest.entries.iter().position(|entry| entry.id == id) else {
                return Ok(None);
            };
            let removed = manifest.entries.remove(index);
            manifest.write_unlocked(vault_root)?;
            Ok(Some(removed))
        })
    }

    pub fn update(
        vault_root: &Path,
        action: impl FnOnce(&mut SourceManifest) -> Result<bool, WikiError>,
    ) -> Result<(), WikiError> {
        with_manifest_lock(vault_root, || {
            let mut manifest = Self::read(vault_root)?;
            if action(&mut manifest)? {
                manifest.write_unlocked(vault_root)?;
            }
            Ok(())
        })
    }

    pub fn index_path(vault_root: &Path) -> PathBuf {
        vault_root.join("raw").join("INDEX.md")
    }
}

fn with_manifest_lock<T>(
    vault_root: &Path,
    action: impl FnOnce() -> Result<T, WikiError>,
) -> Result<T, WikiError> {
    let lock_path = vault_root.join(".gwiki").join("source-manifest.lock");
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create source manifest lock directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)
        .map_err(|error| WikiError::Io {
            action: "open source manifest lock",
            path: Some(lock_path.clone()),
            source: error,
        })?;
    lock_source_manifest(&lock, &lock_path)?;
    let action_result = action();
    let unlock_result = fs4::FileExt::unlock(&lock).map_err(|error| WikiError::Io {
        action: "unlock source manifest",
        path: Some(lock_path),
        source: error,
    });

    match action_result {
        Ok(value) => unlock_result.map(|()| value),
        Err(error) => {
            let _ = unlock_result;
            Err(error)
        }
    }
}

fn lock_source_manifest(lock: &File, lock_path: &Path) -> Result<(), WikiError> {
    let timeout = source_manifest_lock_timeout();
    let started = Instant::now();

    loop {
        match try_lock_exclusive(lock) {
            Ok(()) => return Ok(()),
            Err(fs4::TryLockError::WouldBlock) => {
                let elapsed = started.elapsed();
                if elapsed >= timeout {
                    return Err(WikiError::Io {
                        action: "lock source manifest",
                        path: Some(lock_path.to_path_buf()),
                        source: std::io::Error::new(
                            ErrorKind::TimedOut,
                            format!("timed out after {}ms", timeout.as_millis()),
                        ),
                    });
                }
                thread::sleep(SOURCE_MANIFEST_LOCK_RETRY_DELAY.min(timeout - elapsed));
            }
            Err(error) => {
                return Err(WikiError::Io {
                    action: "lock source manifest",
                    path: Some(lock_path.to_path_buf()),
                    source: error.into(),
                });
            }
        }
    }
}

fn try_lock_exclusive(lock: &File) -> Result<(), fs4::TryLockError> {
    // fs4 names the exclusive lock attempt `try_lock`; shared locking is
    // exposed separately as `try_lock_shared`.
    fs4::FileExt::try_lock(lock)
}

fn source_manifest_lock_timeout() -> Duration {
    std::env::var(SOURCE_MANIFEST_LOCK_TIMEOUT_ENV)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| *value > 0)
        .map(Duration::from_millis)
        .unwrap_or(DEFAULT_SOURCE_MANIFEST_LOCK_TIMEOUT)
}

struct SourceRecordParts {
    location: String,
    kind: SourceKind,
    fetched_at: String,
    title: Option<String>,
    citation: Option<String>,
    license: Option<String>,
    ingestion_method: IngestionMethod,
    compile_status: CompileStatus,
}
