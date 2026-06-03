//! Source manifest records for immutable raw wiki sources.

use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::thread;
use std::time::{Duration, Instant};

use gobby_core::config::AiRouting;
use serde::{Deserialize, Serialize};

use crate::{IngestFileOptions, WikiError};

const SOURCE_ID_HASH_PREFIX_LEN: usize = 16;
const SOURCE_MANIFEST_LOCK_TIMEOUT_ENV: &str = "GWIKI_SOURCE_MANIFEST_LOCK_TIMEOUT_MS";
const DEFAULT_SOURCE_MANIFEST_LOCK_TIMEOUT: Duration = Duration::from_secs(30);
const SOURCE_MANIFEST_LOCK_RETRY_DELAY: Duration = Duration::from_millis(25);

const SOURCE_MARKER: &str = "<!-- gwiki-source:";
const GENERATED_SOURCE_MANIFEST_START: &str = "<!-- GENERATED SOURCE MANIFEST START -->";
const GENERATED_SOURCE_MANIFEST_END: &str = "<!-- GENERATED SOURCE MANIFEST END -->";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceKind {
    Url,
    Audio,
    Image,
    Video,
    Pdf,
    Office,
    Html,
    Markdown,
    Text,
    File,
    Stdin,
    ResearchNote,
    #[serde(rename = "mediawiki")]
    MediaWiki,
    Wayback,
    GitRepository,
}

impl fmt::Display for SourceKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Url => "url",
            Self::Audio => "audio",
            Self::Image => "image",
            Self::Video => "video",
            Self::Pdf => "pdf",
            Self::Office => "office",
            Self::Html => "html",
            Self::Markdown => "markdown",
            Self::Text => "text",
            Self::File => "file",
            Self::Stdin => "stdin",
            Self::ResearchNote => "research_note",
            Self::MediaWiki => "mediawiki",
            Self::Wayback => "wayback",
            Self::GitRepository => "git_repository",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IngestionMethod {
    Manual,
    Research,
}

impl fmt::Display for IngestionMethod {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Manual => "manual",
            Self::Research => "research",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompileStatus {
    Pending,
    Compiled,
}

impl fmt::Display for CompileStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Pending => "pending",
            Self::Compiled => "compiled",
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceDraft {
    pub location: String,
    pub kind: SourceKind,
    pub fetched_at: String,
    pub content: Vec<u8>,
    pub title: Option<String>,
    pub citation: Option<String>,
    pub license: Option<String>,
    pub ingestion_method: IngestionMethod,
    pub compile_status: CompileStatus,
}

impl SourceDraft {
    pub fn url(
        location: impl Into<String>,
        fetched_at: impl Into<String>,
        content: impl Into<Vec<u8>>,
    ) -> Self {
        Self {
            location: location.into(),
            kind: SourceKind::Url,
            fetched_at: fetched_at.into(),
            content: content.into(),
            title: None,
            citation: None,
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        }
    }

    pub fn with_title(mut self, title: impl Into<String>) -> Self {
        self.title = Some(title.into());
        self
    }

    pub fn with_citation(mut self, citation: impl Into<String>) -> Self {
        self.citation = Some(citation.into());
        self
    }

    pub fn with_license(mut self, license: impl Into<String>) -> Self {
        self.license = Some(license.into());
        self
    }
}

pub(crate) struct SourceDraftRef<'a> {
    pub location: String,
    pub kind: SourceKind,
    pub fetched_at: String,
    pub content: &'a [u8],
    pub title: Option<String>,
    pub citation: Option<String>,
    pub license: Option<String>,
    pub ingestion_method: IngestionMethod,
    pub compile_status: CompileStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceRecord {
    pub id: String,
    pub location: String,
    pub canonical_location: String,
    pub kind: SourceKind,
    pub fetched_at: String,
    pub content_hash: String,
    pub title: Option<String>,
    pub citation: Option<String>,
    pub license: Option<String>,
    pub ingestion_method: IngestionMethod,
    pub compile_status: CompileStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replay: Option<SourceReplay>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SourceReplay {
    LocalFile {
        path: PathBuf,
        #[serde(default)]
        options: SourceReplayOptions,
    },
}

impl SourceReplay {
    pub(crate) fn local_file(path: PathBuf, options: &IngestFileOptions) -> Self {
        Self::LocalFile {
            path,
            options: SourceReplayOptions::from_ingest_file_options(options),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceReplayOptions {
    #[serde(default, skip_serializing_if = "is_false")]
    pub no_ai: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub translate: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_lang: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub video_frame_interval_seconds: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub transcription_routing: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub vision_routing: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text_routing: Option<String>,
}

impl SourceReplayOptions {
    pub(crate) fn from_ingest_file_options(options: &IngestFileOptions) -> Self {
        Self {
            no_ai: options.no_ai,
            translate: options.translate,
            target_lang: options.target_lang.clone(),
            video_frame_interval_seconds: options.video_frame_interval_seconds,
            transcription_routing: options.transcription_routing.map(routing_name),
            vision_routing: options.vision_routing.map(routing_name),
            text_routing: options.text_routing.map(routing_name),
        }
    }

    pub(crate) fn to_ingest_file_options(&self) -> Result<IngestFileOptions, WikiError> {
        Ok(IngestFileOptions {
            no_ai: self.no_ai,
            translate: self.translate,
            target_lang: self.target_lang.clone(),
            video_frame_interval_seconds: self.video_frame_interval_seconds,
            transcription_routing: parse_routing(
                "transcription_routing",
                &self.transcription_routing,
            )?,
            vision_routing: parse_routing("vision_routing", &self.vision_routing)?,
            text_routing: parse_routing("text_routing", &self.text_routing)?,
        })
    }
}

fn is_false(value: &bool) -> bool {
    !*value
}

fn routing_name(routing: AiRouting) -> String {
    match routing {
        AiRouting::Auto => "auto",
        AiRouting::Daemon => "daemon",
        AiRouting::Direct => "direct",
        AiRouting::Off => "off",
    }
    .to_string()
}

fn parse_routing(
    field: &'static str,
    value: &Option<String>,
) -> Result<Option<AiRouting>, WikiError> {
    value
        .as_deref()
        .map(|value| {
            AiRouting::from_str(value).map_err(|error| WikiError::InvalidInput {
                field,
                message: error.to_string(),
            })
        })
        .transpose()
}

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

    /// Run an action while holding the source manifest lock.
    ///
    /// Callers must not invoke public APIs that acquire this same lock from inside
    /// the action. Methods on `SourceManifest` use `write_unlocked` internally
    /// after acquiring the lock.
    pub(crate) fn with_lock<T>(
        vault_root: &Path,
        action: impl FnOnce() -> Result<T, WikiError>,
    ) -> Result<T, WikiError> {
        with_manifest_lock(vault_root, action)
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
        match fs4::FileExt::try_lock(lock) {
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

fn render_entry(entry: &SourceRecord, index: &mut String) -> Result<(), WikiError> {
    let title = entry.title.as_deref().unwrap_or(&entry.location);
    index.push_str("- [");
    index.push_str(&escape_markdown_text(title));
    index.push_str("](");
    index.push_str(&escape_markdown_destination(&entry.location));
    index.push_str(")\n");
    index.push_str("  - id: `");
    index.push_str(&entry.id);
    index.push_str("`\n");
    index.push_str("  - canonical: `");
    index.push_str(&entry.canonical_location);
    index.push_str("`\n");
    index.push_str("  - kind: `");
    index.push_str(&entry.kind.to_string());
    index.push_str("`\n");
    index.push_str("  - fetched_at: `");
    index.push_str(&entry.fetched_at);
    index.push_str("`\n");
    index.push_str("  - hash: `");
    index.push_str(&entry.content_hash);
    index.push_str("`\n");
    if let Some(citation) = &entry.citation {
        index.push_str("  - citation: ");
        index.push_str(&inline_text(citation));
        index.push('\n');
    }
    if let Some(license) = &entry.license {
        index.push_str("  - license: ");
        index.push_str(&inline_text(license));
        index.push('\n');
    }
    index.push_str("  - ingestion_method: `");
    index.push_str(&entry.ingestion_method.to_string());
    index.push_str("`\n");
    index.push_str("  - compile_status: `");
    index.push_str(&entry.compile_status.to_string());
    index.push_str("`\n");

    let metadata = serde_json::to_string(entry).map_err(|error| WikiError::Json {
        action: "serialize raw source index marker",
        path: None,
        source: error,
    })?;
    index.push_str("  - ");
    index.push_str(SOURCE_MARKER);
    index.push_str(&metadata);
    index.push_str(" -->\n");
    Ok(())
}

fn canonicalize_location(location: &str) -> String {
    let without_fragment = location.trim().split('#').next().unwrap_or("").trim();
    let canonical = lower_url_scheme_and_authority(without_fragment);
    let (mut base, query) = split_sorted_query(&canonical);
    while base.ends_with('/') && base != "/" && !base.ends_with("://") {
        base.pop();
    }
    match query {
        Some(query) if !query.is_empty() => format!("{base}?{query}"),
        _ => base,
    }
}

fn split_sorted_query(location: &str) -> (String, Option<String>) {
    let Some((base, query)) = location.split_once('?') else {
        return (location.to_string(), None);
    };
    let mut params = query
        .split('&')
        .filter(|param| !param.is_empty())
        .collect::<Vec<_>>();
    params.sort_unstable();
    (base.to_string(), Some(params.join("&")))
}

struct PreservedSourceIndex {
    prefix: String,
    suffix: String,
}

fn existing_index_without_manifest(index_path: &Path) -> Result<PreservedSourceIndex, WikiError> {
    if !index_path.exists() {
        return Ok(PreservedSourceIndex {
            prefix: String::from("# Raw Sources\n\n"),
            suffix: String::new(),
        });
    }

    let existing = fs::read_to_string(index_path).map_err(|error| WikiError::Io {
        action: "read raw source index",
        path: Some(index_path.to_path_buf()),
        source: error,
    })?;

    if let Some(start) = existing.find(GENERATED_SOURCE_MANIFEST_START) {
        let search_from = start + GENERATED_SOURCE_MANIFEST_START.len();
        if let Some(relative_end) = existing[search_from..].find(GENERATED_SOURCE_MANIFEST_END) {
            let end = search_from + relative_end + GENERATED_SOURCE_MANIFEST_END.len();
            return Ok(PreservedSourceIndex {
                prefix: normalize_preserved_index_prefix(&existing[..start]),
                suffix: normalize_preserved_index_suffix(&existing[end..]),
            });
        }
    }

    if let Some(start) = existing.find("\n## Source manifest") {
        return Ok(PreservedSourceIndex {
            prefix: normalize_preserved_index_prefix(&existing[..start]),
            suffix: String::new(),
        });
    }
    if existing.trim_start().starts_with("## Source manifest") {
        return Ok(PreservedSourceIndex {
            prefix: String::from("# Raw Sources\n\n"),
            suffix: String::new(),
        });
    }

    Ok(PreservedSourceIndex {
        prefix: normalize_preserved_index_prefix(&existing),
        suffix: String::new(),
    })
}

fn normalize_preserved_index_prefix(prefix: &str) -> String {
    let mut prefix = prefix.trim_end_matches('\n').to_string();
    if prefix.trim().is_empty() {
        prefix.push_str("# Raw Sources");
    }
    prefix.push_str("\n\n");
    prefix
}

fn normalize_preserved_index_suffix(suffix: &str) -> String {
    suffix.trim_start_matches('\n').to_string()
}

fn write_atomic(path: &Path, contents: &[u8], action: &'static str) -> Result<(), WikiError> {
    let temp_path = temp_sibling_path(path);
    let mut file = File::create(&temp_path).map_err(|error| WikiError::Io {
        action: "create raw source index temp file",
        path: Some(temp_path.clone()),
        source: error,
    })?;
    if let Err(error) = file.write_all(contents) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action,
            path: Some(temp_path),
            source: error,
        });
    }
    if let Err(error) = file.sync_all() {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action: "sync raw source index temp file",
            path: Some(temp_path),
            source: error,
        });
    }
    drop(file);
    if let Err(error) = replace_atomic(&temp_path, path) {
        let _ = fs::remove_file(&temp_path);
        return Err(WikiError::Io {
            action,
            path: Some(path.to_path_buf()),
            source: error,
        });
    }
    sync_parent_dir(path)
}

fn replace_atomic(temp_path: &Path, path: &Path) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
    }
    fs::rename(temp_path, path)
}

fn temp_sibling_path(path: &Path) -> PathBuf {
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("INDEX.md");
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    path.with_file_name(format!(".{file_name}.{}.{nanos}.tmp", std::process::id()))
}

fn sync_parent_dir(path: &Path) -> Result<(), WikiError> {
    #[cfg(not(unix))]
    {
        let _ = path;
        Ok(())
    }
    #[cfg(unix)]
    {
        let Some(parent) = path.parent() else {
            return Ok(());
        };
        File::open(parent)
            .and_then(|dir| dir.sync_all())
            .map_err(|error| WikiError::Io {
                action: "sync raw source index directory",
                path: Some(parent.to_path_buf()),
                source: error,
            })
    }
}

fn lower_url_scheme_and_authority(location: &str) -> String {
    let Some((scheme, rest)) = location.split_once("://") else {
        return location.replace('\\', "/");
    };
    let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
    if path.is_empty() {
        format!(
            "{}://{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase()
        )
    } else {
        format!(
            "{}://{}/{}",
            scheme.to_ascii_lowercase(),
            authority.to_ascii_lowercase(),
            path
        )
    }
}

fn source_id(canonical_location: &str, content_hash: &str) -> String {
    // Sixteen hex chars gives a 64-bit collision space while keeping source IDs
    // readable in Markdown manifests.
    let hash_prefix = &content_hash[..content_hash.len().min(SOURCE_ID_HASH_PREFIX_LEN)];
    let mut slug = String::new();
    let mut last_was_dash = false;
    for ch in canonical_location.chars().flat_map(char::to_lowercase) {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            last_was_dash = false;
        } else if !last_was_dash && !slug.is_empty() {
            slug.push('-');
            last_was_dash = true;
        }
        if slug.len() >= 48 {
            break;
        }
    }
    while slug.ends_with('-') {
        slug.pop();
    }

    if slug.is_empty() {
        format!("src-{hash_prefix}")
    } else {
        format!("src-{hash_prefix}-{slug}")
    }
}

fn escape_markdown_text(text: &str) -> String {
    text.replace('[', "\\[").replace(']', "\\]")
}

fn escape_markdown_destination(destination: &str) -> String {
    destination.replace('(', "\\(").replace(')', "\\)")
}

fn inline_text(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use gobby_core::config::AiRouting;

    #[test]
    fn dedupes_by_canonical_identity_and_hash() {
        let temp = tempfile::tempdir().expect("tempdir");
        let content = b"Source material about durable wiki provenance.".to_vec();

        let first = SourceManifest::register(
            temp.path(),
            SourceDraft::url(
                "https://Example.com/docs/provenance#chunk-1",
                "2026-05-29T15:00:00Z",
                content.clone(),
            )
            .with_title("Durable provenance")
            .with_citation("Example Docs, Durable provenance")
            .with_license("Apache-2.0"),
        )
        .expect("first source registered");
        let duplicate = SourceManifest::register(
            temp.path(),
            SourceDraft::url(
                "https://example.com/docs/provenance/",
                "2026-05-29T16:00:00Z",
                content,
            )
            .with_title("Duplicate durable provenance")
            .with_citation("Duplicate citation should not replace record")
            .with_license("MIT"),
        )
        .expect("duplicate source reused");

        assert_eq!(duplicate, first);

        let index = std::fs::read_to_string(SourceManifest::index_path(temp.path()))
            .expect("raw index written");
        assert_eq!(index.matches("gwiki-source:").count(), 1);
        assert!(index.contains("https://Example.com/docs/provenance#chunk-1"));
        assert!(index.contains("kind: `url`"));
        assert!(index.contains("fetched_at: `2026-05-29T15:00:00Z`"));
        assert!(index.contains(&first.content_hash));
        assert!(index.contains("citation: Example Docs, Durable provenance"));
        assert!(index.contains("license: Apache-2.0"));
        assert!(index.contains("ingestion_method: `manual`"));
        assert!(index.contains("compile_status: `pending`"));
    }

    #[test]
    fn local_file_replay_metadata_round_trips_through_manifest() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = SourceManifest::register(
            temp.path(),
            SourceDraft {
                location: "notes/source.md".to_string(),
                kind: SourceKind::Markdown,
                fetched_at: "2026-06-02T00:00:00Z".to_string(),
                content: b"# Source\n".to_vec(),
                title: Some("Source".to_string()),
                citation: Some("notes/source.md".to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register source");
        let options = IngestFileOptions {
            no_ai: true,
            translate: true,
            target_lang: Some("es".to_string()),
            video_frame_interval_seconds: Some(11),
            transcription_routing: Some(AiRouting::Direct),
            vision_routing: Some(AiRouting::Off),
            text_routing: Some(AiRouting::Daemon),
        };
        let replay = SourceReplay::local_file(PathBuf::from("notes/source.md"), &options);

        SourceManifest::update(temp.path(), |manifest| {
            manifest.entries[0].replay = Some(replay);
            Ok(true)
        })
        .expect("write replay metadata");

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].id, record.id);
        let Some(SourceReplay::LocalFile {
            path,
            options: replay_options,
        }) = &manifest.entries[0].replay
        else {
            panic!("expected local file replay");
        };
        assert_eq!(path, &PathBuf::from("notes/source.md"));
        assert_eq!(
            replay_options.transcription_routing.as_deref(),
            Some("direct")
        );
        assert_eq!(replay_options.vision_routing.as_deref(), Some("off"));
        assert_eq!(replay_options.text_routing.as_deref(), Some("daemon"));

        let restored = replay_options
            .to_ingest_file_options()
            .expect("replay options parse");
        assert_eq!(restored, options);
        let index = std::fs::read_to_string(SourceManifest::index_path(temp.path()))
            .expect("raw index written");
        assert!(index.contains("\"replay\""));
        assert!(index.contains("\"kind\":\"local_file\""));
    }

    #[test]
    fn canonical_location_sorts_query_before_trimming_slash() {
        assert_eq!(
            canonicalize_location("https://Example.com/docs/?b=2&a=1#frag"),
            "https://example.com/docs?a=1&b=2"
        );
    }

    #[test]
    fn existing_index_strips_manifest_through_following_headings() {
        let temp = tempfile::tempdir().expect("tempdir");
        let index_path = SourceManifest::index_path(temp.path());
        std::fs::create_dir_all(index_path.parent().expect("raw dir")).expect("raw dir");
        std::fs::write(
            &index_path,
            "# Raw Sources\n\nManual note.\n\n## Source manifest\n\n- generated\n\n## Generated Heading\n\n- stale generated content\n",
        )
        .expect("index");

        let stripped = existing_index_without_manifest(&index_path).expect("strip manifest");

        assert!(stripped.prefix.contains("Manual note."));
        assert!(!stripped.prefix.contains("Generated Heading"));
        assert!(!stripped.prefix.contains("stale generated content"));
        assert!(stripped.suffix.is_empty());
    }

    #[test]
    fn existing_index_preserves_content_after_marked_manifest() {
        let temp = tempfile::tempdir().expect("tempdir");
        let index_path = SourceManifest::index_path(temp.path());
        std::fs::create_dir_all(index_path.parent().expect("raw dir")).expect("raw dir");
        std::fs::write(
            &index_path,
            format!(
                "# Raw Sources\n\nManual before.\n\n{GENERATED_SOURCE_MANIFEST_START}\n## Source manifest\n\n- generated\n{GENERATED_SOURCE_MANIFEST_END}\n\nManual after.\n",
            ),
        )
        .expect("index");

        let stripped = existing_index_without_manifest(&index_path).expect("strip manifest");

        assert!(stripped.prefix.contains("Manual before."));
        assert!(!stripped.prefix.contains("generated"));
        assert!(stripped.suffix.contains("Manual after."));
    }
}
