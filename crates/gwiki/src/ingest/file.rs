use std::path::Path;

use gobby_core::ai_context::AiContext;
#[cfg(all(feature = "documents", feature = "ai"))]
use gobby_core::config::AiCapability;
use gobby_core::config::AiRouting;

#[cfg(all(feature = "documents", feature = "ai"))]
use crate::ai::clients::ProductionVisionClient;
use crate::api::IngestFileOptions;
use crate::ingest::audio::{
    AudioSnapshot, ingest_audio_with_transcription_without_index, production_transcription_endpoint,
};
#[cfg(feature = "documents")]
use crate::ingest::document::{DocumentSnapshot, ingest_document_without_index};
use crate::ingest::image::{ImageSnapshot, ingest_image_with_production_vision_without_index};
#[cfg(feature = "documents")]
use crate::ingest::pdf::{
    PdfFileSnapshot, PdfIngestOptions, ingest_pdf_file_without_index, pdf_fetched_at,
};
use crate::ingest::video::{
    VideoFileSnapshot, VideoIngestResult,
    ingest_video_file_with_production_processing_without_index,
};
use crate::ingest::{
    IngestResult, index_after_ingest, lowercase_extension, markdown_metadata, markdown_title,
    path_to_string, text_from_utf8_lossy, write_asset, write_raw_markdown,
};
use crate::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceDraftRef, SourceKind, SourceManifest,
    SourceReplay,
};
use crate::store::WikiIndexStore;
use crate::vision::VisionDegradation;
#[cfg(feature = "documents")]
use crate::vision::VisionEndpoint;
use crate::{ScopeIdentity, WikiError};
#[cfg(all(feature = "documents", feature = "ai"))]
use gobby_core::ai::effective_route;

/// TEXT_INLINE_LIMIT_BYTES keeps ordinary text inline while pushing unusually
/// large text into raw assets. The 256 KB threshold is above typical note and
/// config sizes but below the point where markdown rendering, chunking, memory
/// use, search indexing, and API transfer costs start to dominate an ingest.
/// Lower it in production if inline text drives slow indexing or high network
/// transfer; raise it only when metrics show asset indirection is costing more
/// than the extra memory and I/O.
const TEXT_INLINE_LIMIT_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StdinSnapshot {
    pub label: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LocalFileIngestResult {
    pub result: IngestResult,
    pub degradations: Vec<String>,
}

pub fn ingest_path(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: &ScopeIdentity,
    ai_context: &AiContext,
    options: &IngestFileOptions,
    path: &Path,
    fetched_at: &str,
) -> Result<IngestResult, WikiError> {
    let result =
        ingest_path_without_index(vault_root, scope, ai_context, options, path, fetched_at)?;
    index_after_ingest(vault_root, store)?;
    Ok(result.result)
}

pub(crate) fn ingest_path_without_index(
    vault_root: &Path,
    scope: &ScopeIdentity,
    ai_context: &AiContext,
    options: &IngestFileOptions,
    path: &Path,
    fetched_at: &str,
) -> Result<LocalFileIngestResult, WikiError> {
    let kind = detect_source_kind(path);
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("source");
    let location = source_location(vault_root, path);
    let mut local_result = match kind {
        SourceKind::Audio => {
            let bytes = read_source_file(path)?;
            let result = ingest_audio_with_transcription_without_index(
                vault_root,
                scope.clone(),
                AudioSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    mime_type: None,
                    duration_seconds: None,
                },
                production_transcription_endpoint(ai_context, options.translate),
            )?;
            let degradations = result
                .transcription_degradation
                .as_ref()
                .map(transcription_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        SourceKind::Image => {
            let bytes = read_source_file(path)?;
            let result = ingest_image_with_production_vision_without_index(
                vault_root,
                scope.clone(),
                ai_context,
                ImageSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    mime_type: None,
                    width: None,
                    height: None,
                },
            )?;
            let degradations = result
                .vision_degradation
                .as_ref()
                .map(vision_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        SourceKind::Video => {
            let result = ingest_video_file_with_production_processing_without_index(
                vault_root,
                scope.clone(),
                ai_context,
                VideoFileSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    path: path.to_path_buf(),
                    mime_type: None,
                    duration_seconds: None,
                    frame_interval_seconds: options.video_frame_interval_seconds,
                    frame_samples: Vec::new(),
                    frame_image_paths: Vec::new(),
                    frame_descriptions: Vec::new(),
                    transcript_segments: Vec::new(),
                    transcription: None,
                },
                options.translate,
            )?;
            let degradations = video_degradation_summaries(&result);
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        #[cfg(feature = "documents")]
        SourceKind::Pdf => {
            let bytes = read_source_file(path)?;
            let snapshot = PdfFileSnapshot {
                location,
                file_name: file_name.to_string(),
                fetched_at: pdf_fetched_at(fetched_at)?,
                bytes,
            };
            #[cfg(feature = "ai")]
            {
                let client = (effective_route(ai_context, AiCapability::VisionExtract)
                    != AiRouting::Off)
                    .then(|| ProductionVisionClient::new(ai_context.clone()));
                let endpoint = client
                    .as_ref()
                    .map(|client| {
                        VisionEndpoint::Available(client as &dyn crate::vision::VisionClient)
                    })
                    .unwrap_or_else(|| {
                        VisionEndpoint::Unavailable(vision_degradation(
                            ai_context.binding(AiCapability::VisionExtract).routing,
                        ))
                    });
                let result = ingest_pdf_file_without_index(
                    vault_root,
                    scope,
                    snapshot,
                    endpoint,
                    PdfIngestOptions::default(),
                )?;
                LocalFileIngestResult {
                    result,
                    degradations: Vec::new(),
                }
            }
            #[cfg(not(feature = "ai"))]
            {
                let result = ingest_pdf_file_without_index(
                    vault_root,
                    scope,
                    snapshot,
                    VisionEndpoint::Unavailable(vision_degradation(AiRouting::Off)),
                    PdfIngestOptions::default(),
                )?;
                LocalFileIngestResult {
                    result,
                    degradations: Vec::new(),
                }
            }
        }
        #[cfg(feature = "documents")]
        SourceKind::Office | SourceKind::Html => {
            let bytes = read_source_file(path)?;
            let result = ingest_document_without_index(
                vault_root,
                scope.clone(),
                DocumentSnapshot {
                    location,
                    file_name: file_name.to_string(),
                    fetched_at: fetched_at.to_string(),
                    bytes,
                    kind,
                },
            )?;
            let degradations = result
                .document_degradation
                .as_ref()
                .map(document_degradation_summary)
                .into_iter()
                .collect();
            LocalFileIngestResult {
                result: result.into(),
                degradations,
            }
        }
        _ => ingest_generic_file_without_index(
            vault_root, &kind, file_name, &location, path, fetched_at,
        )?,
    };

    attach_replay_metadata(vault_root, &mut local_result.result, path, options)?;
    Ok(local_result)
}

fn ingest_generic_file_without_index(
    vault_root: &Path,
    kind: &SourceKind,
    file_name: &str,
    location: &str,
    path: &Path,
    fetched_at: &str,
) -> Result<LocalFileIngestResult, WikiError> {
    let bytes = read_source_file(path)?;
    let title = markdown_title(file_name);
    let record = SourceManifest::register_borrowed(
        vault_root,
        SourceDraftRef {
            location: location.to_string(),
            kind: kind.clone(),
            fetched_at: fetched_at.to_string(),
            content: &bytes,
            title: Some(title.clone()),
            citation: Some(location.to_string()),
            license: None,
            ingestion_method: IngestionMethod::Manual,
            compile_status: CompileStatus::Pending,
        },
    )?;
    let asset_path = should_store_asset(kind, bytes.len())
        .then(|| write_asset(vault_root, &record, file_name, &bytes))
        .transpose()?;
    let markdown = render_file_markdown(
        &title,
        location,
        fetched_at,
        &record.content_hash,
        kind,
        &bytes,
        asset_path.as_deref(),
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;

    Ok(LocalFileIngestResult {
        result: IngestResult {
            record,
            raw_path,
            asset_path,
        },
        degradations: Vec::new(),
    })
}

fn attach_replay_metadata(
    vault_root: &Path,
    result: &mut IngestResult,
    path: &Path,
    options: &IngestFileOptions,
) -> Result<(), WikiError> {
    let replay = SourceReplay::local_file(path.to_path_buf(), options);
    SourceManifest::update(vault_root, |manifest| {
        let Some(entry) = manifest
            .entries
            .iter_mut()
            .find(|entry| entry.id == result.record.id)
        else {
            result.record.replay = Some(replay.clone());
            return Ok(false);
        };
        if entry.replay.as_ref() == Some(&replay) {
            result.record.replay = Some(replay);
            return Ok(false);
        }
        entry.replay = Some(replay.clone());
        result.record.replay = Some(replay);
        Ok(true)
    })
}

fn transcription_degradation_summary(
    degradation: &crate::transcribe::TranscriptionDegradation,
) -> String {
    format!(
        "audio_transcription:{}:{}",
        degradation.reason, degradation.fallback
    )
}

fn vision_degradation_summary(degradation: &VisionDegradation) -> String {
    format!("vision:{}:{}", degradation.reason, degradation.fallback)
}

#[cfg(feature = "documents")]
fn document_degradation_summary(degradation: &crate::document::DocumentDegradation) -> String {
    format!("document:{}:{}", degradation.reason(), degradation.fallback)
}

fn video_degradation_summaries(result: &VideoIngestResult) -> Vec<String> {
    let mut degradations = result
        .media_degradations
        .iter()
        .map(|degradation| {
            format!(
                "video_{}:{}:{}",
                degradation.kind, degradation.reason, degradation.message
            )
        })
        .collect::<Vec<_>>();
    if let Some(degradation) = &result.transcription_degradation {
        degradations.push(transcription_degradation_summary(degradation));
    }
    degradations
}

fn read_source_file(path: &Path) -> Result<Vec<u8>, WikiError> {
    std::fs::read(path).map_err(|error| WikiError::Io {
        action: "read source file",
        path: Some(path.to_path_buf()),
        source: error,
    })
}

pub fn ingest_stdin(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    snapshot: StdinSnapshot,
) -> Result<IngestResult, WikiError> {
    let title = markdown_title(&snapshot.label);
    let location = format!("stdin:{}", snapshot.label);
    let draft = SourceDraft::new(
        location.clone(),
        SourceKind::Stdin,
        snapshot.fetched_at.clone(),
        snapshot.bytes.clone(),
    )
    .with_title(title.clone());
    let record = SourceManifest::register(vault_root, draft)?;
    let markdown = render_file_markdown(
        &title,
        &location,
        &snapshot.fetched_at,
        &record.content_hash,
        &SourceKind::Stdin,
        &snapshot.bytes,
        None,
    );
    let raw_path = write_raw_markdown(vault_root, &record, &markdown)?;
    index_after_ingest(vault_root, store)?;

    Ok(IngestResult {
        record,
        raw_path,
        asset_path: None,
    })
}

fn detect_source_kind(path: &Path) -> SourceKind {
    match lowercase_extension(path).as_deref() {
        Some("pdf") => SourceKind::Pdf,
        Some("docx" | "xlsx" | "xls" | "ods" | "pptx") => SourceKind::Office,
        Some("html" | "htm") => SourceKind::Html,
        Some("mp3" | "wav" | "m4a" | "aac" | "flac" | "ogg" | "opus") => SourceKind::Audio,
        Some("png" | "jpg" | "jpeg" | "gif" | "webp" | "bmp" | "tiff") => SourceKind::Image,
        Some("mp4" | "mov" | "m4v" | "webm" | "mkv") => SourceKind::Video,
        Some("md" | "markdown") => SourceKind::Markdown,
        Some(
            "txt" | "text" | "csv" | "json" | "jsonl" | "xml" | "yaml" | "yml" | "toml" | "log"
            | "ini" | "env" | "properties" | "conf" | "sql" | "sh" | "bash",
        ) => SourceKind::Text,
        _ => SourceKind::File,
    }
}

fn source_location(vault_root: &Path, path: &Path) -> String {
    let display_path = if let Ok(relative) = path.strip_prefix(vault_root) {
        relative.to_path_buf()
    } else if let (Ok(canonical_root), Ok(canonical_path)) =
        (vault_root.canonicalize(), path.canonicalize())
    {
        canonical_path
            .strip_prefix(&canonical_root)
            .map(Path::to_path_buf)
            .unwrap_or(canonical_path)
    } else {
        path.to_path_buf()
    };
    display_path.to_string_lossy().replace('\\', "/")
}

fn should_store_asset(kind: &SourceKind, byte_len: usize) -> bool {
    matches!(kind, SourceKind::Text if byte_len > TEXT_INLINE_LIMIT_BYTES)
        || matches!(
            kind,
            SourceKind::Audio
                | SourceKind::Image
                | SourceKind::Video
                | SourceKind::Pdf
                | SourceKind::Office
                | SourceKind::Html
                | SourceKind::File
        )
}

fn render_file_markdown(
    title: &str,
    location: &str,
    fetched_at: &str,
    source_hash: &str,
    kind: &SourceKind,
    bytes: &[u8],
    asset_path: Option<&Path>,
) -> String {
    let mut fields = vec![
        ("source_kind", kind.to_string()),
        ("source_location", location.to_string()),
        ("fetched_at", fetched_at.to_string()),
        ("source_hash", source_hash.to_string()),
    ];
    if let Some(asset_path) = asset_path {
        fields.push(("source_asset", path_to_string(asset_path)));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(title);
    markdown.push_str("\n\n");

    match (kind, asset_path) {
        (SourceKind::Markdown | SourceKind::Text | SourceKind::Stdin, None) => {
            markdown.push_str(&text_from_utf8_lossy(bytes));
            if !markdown.ends_with('\n') {
                markdown.push('\n');
            }
        }
        _ => {
            if let Some(asset_path) = asset_path {
                markdown.push_str("Original artifact stored under `");
                markdown.push_str(&path_to_string(asset_path));
                markdown.push_str("`.\n");
            } else {
                markdown.push_str(
                    "Original artifact was recorded in the source manifest; no raw asset was written.\n",
                );
            }
        }
    }

    markdown
}

fn vision_degradation(routing: AiRouting) -> VisionDegradation {
    let reason = match routing {
        AiRouting::Off => "disabled",
        AiRouting::Auto | AiRouting::Daemon | AiRouting::Direct => "missing_endpoint",
    };
    VisionDegradation {
        reason: reason.to_string(),
        fallback: "Keep PDF text layer only; skip page raster vision.".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use gobby_core::ai_context::AiContext;
    use gobby_core::config::EnvOnlySource;
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::api::IngestFileOptions;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::MemoryWikiStore;

    fn no_ai_context() -> AiContext {
        let mut source = EnvOnlySource;
        let mut context = AiContext::resolve(None, &mut source);
        IngestFileOptions {
            no_ai: true,
            ..IngestFileOptions::default()
        }
        .apply_to_ai_context(&mut context);
        context
    }

    fn ingest_options() -> IngestFileOptions {
        IngestFileOptions {
            no_ai: true,
            video_frame_interval_seconds: Some(0),
            ..IngestFileOptions::default()
        }
    }

    #[test]
    fn source_location_preserves_external_canonical_path() {
        let vault = tempfile::tempdir().expect("vault tempdir");
        let outside = tempfile::tempdir().expect("outside tempdir");
        let source = outside.path().join("source.md");
        std::fs::write(&source, "# Source\n").expect("write outside source");

        let location = source_location(vault.path(), &source);

        assert_eq!(
            location,
            source
                .canonicalize()
                .unwrap()
                .to_string_lossy()
                .replace('\\', "/")
        );
    }

    #[test]
    fn file_and_stdin_ingest_hash_sources() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_bytes = b"# Field Notes\n\nLocal markdown source.\n";
        let file_path = temp.path().join("field-notes.md");
        std::fs::write(&file_path, file_bytes).expect("write local file");
        let stdin_bytes = b"stdin source text\n".to_vec();
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let file_result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-29T16:45:00Z",
        )
        .expect("ingest local file");
        let stdin_result = ingest_stdin(
            temp.path(),
            &mut store,
            StdinSnapshot {
                label: "gwiki-stdin".to_string(),
                fetched_at: "2026-05-29T16:46:00Z".to_string(),
                bytes: stdin_bytes.clone(),
            },
        )
        .expect("ingest stdin");

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 2);
        let markdown_entry = manifest
            .entries
            .iter()
            .find(|entry| entry.kind == SourceKind::Markdown)
            .expect("markdown source kind");
        let stdin_entry = manifest
            .entries
            .iter()
            .find(|entry| entry.kind == SourceKind::Stdin)
            .expect("stdin source kind");
        assert_eq!(markdown_entry.content_hash, content_hash(file_bytes));
        assert_eq!(stdin_entry.content_hash, content_hash(&stdin_bytes));

        let raw_file = std::fs::read_to_string(temp.path().join(file_result.raw_path))
            .expect("file raw markdown");
        assert!(raw_file.contains("# Field Notes"));
        let raw_stdin = std::fs::read_to_string(temp.path().join(stdin_result.raw_path))
            .expect("stdin raw markdown");
        assert!(raw_stdin.contains("stdin source text"));
    }

    #[test]
    fn common_audio_extensions_ingest_as_audio_assets() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("interview.mp3");
        std::fs::write(&file_path, b"audio bytes").expect("write local file");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-29T16:47:00Z",
        )
        .expect("ingest audio file");

        assert_eq!(result.record.kind, SourceKind::Audio);
        assert!(result.asset_path.is_some());
    }

    #[test]
    fn detects_audio_and_image_extensions() {
        for extension in ["mp3", "wav", "m4a", "flac", "ogg", "aac", "opus"] {
            assert_eq!(
                detect_source_kind(Path::new(&format!("sample.{extension}"))),
                SourceKind::Audio
            );
        }

        for extension in ["png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff"] {
            assert_eq!(
                detect_source_kind(Path::new(&format!("sample.{extension}"))),
                SourceKind::Image
            );
        }
    }

    #[test]
    fn dispatches_media_to_orchestrators() {
        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        for (name, kind, expected_fragment) in [
            (
                "interview.mp3",
                SourceKind::Audio,
                "## Transcription Unavailable",
            ),
            ("diagram.png", SourceKind::Image, "## Vision Unavailable"),
            ("walkthrough.mp4", SourceKind::Video, "## Frame Samples"),
        ] {
            let path = temp.path().join(name);
            std::fs::write(&path, format!("{name} bytes")).expect("write media file");
            let mut store = MemoryWikiStore::default();

            let result = ingest_path(
                temp.path(),
                &mut store,
                &scope,
                &ai_context,
                &options,
                &path,
                "2026-05-29T16:48:00Z",
            )
            .expect("ingest media file");

            assert_eq!(result.record.kind, kind);
            assert!(result.asset_path.is_some());
            assert!(
                store
                    .documents
                    .values()
                    .any(|document| document.body.contains(expected_fragment)),
                "{name} should be handled by its media orchestrator"
            );
        }
    }

    #[test]
    fn no_ai_dispatch_degrades() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("diagram.png");
        std::fs::write(&file_path, b"image bytes").expect("write local file");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-29T16:49:00Z",
        )
        .expect("ingest image file");

        assert!(result.asset_path.is_some());
        assert!(
            store
                .documents
                .values()
                .any(|document| document.body.contains("## Vision Unavailable"))
        );
    }

    #[test]
    fn media_dispatch_registers_once() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("interview.mp3");
        std::fs::write(&file_path, b"audio bytes").expect("write local file");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-29T16:50:00Z",
        )
        .expect("ingest audio file");

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Audio);
    }

    #[test]
    fn detects_documents_and_inlines_structured_text() {
        assert_eq!(detect_source_kind(Path::new("report.pdf")), SourceKind::Pdf);
        for extension in ["docx", "xlsx", "xls", "ods", "pptx"] {
            assert_eq!(
                detect_source_kind(Path::new(&format!("office.{extension}"))),
                SourceKind::Office
            );
        }
        for extension in ["html", "htm"] {
            assert_eq!(
                detect_source_kind(Path::new(&format!("page.{extension}"))),
                SourceKind::Html
            );
        }
        for extension in [
            "csv",
            "json",
            "jsonl",
            "xml",
            "yaml",
            "yml",
            "toml",
            "log",
            "ini",
            "env",
            "properties",
            "conf",
            "sql",
            "sh",
            "bash",
        ] {
            assert_eq!(
                detect_source_kind(Path::new(&format!("data.{extension}"))),
                SourceKind::Text
            );
        }

        let temp = tempfile::tempdir().expect("tempdir");
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();
        let mut store = MemoryWikiStore::default();

        let small_csv = temp.path().join("data.csv");
        std::fs::write(&small_csv, b"city,count\nDuluth,3\n").expect("write csv");
        let small_result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &small_csv,
            "2026-05-29T16:51:00Z",
        )
        .expect("ingest small csv");
        assert_eq!(small_result.record.kind, SourceKind::Text);
        assert!(small_result.asset_path.is_none());

        let large_json = temp.path().join("large.json");
        std::fs::write(&large_json, vec![b'a'; 262_145]).expect("write large json");
        let large_result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &large_json,
            "2026-05-29T16:52:00Z",
        )
        .expect("ingest large json");
        assert_eq!(large_result.record.kind, SourceKind::Text);
        assert!(large_result.asset_path.is_some());
    }

    #[cfg(feature = "documents")]
    #[test]
    fn dispatches_office_html_to_document() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("page.html");
        std::fs::write(
            &file_path,
            b"<!doctype html><html><head><title>Dispatch Doc</title></head><body><main><p>Document dispatch body.</p></main></body></html>",
        )
        .expect("write html");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-31T20:01:00Z",
        )
        .expect("ingest html");

        assert_eq!(result.record.kind, SourceKind::Html);
        assert!(result.asset_path.is_some());
        assert!(
            store
                .documents
                .values()
                .any(|document| document.body.contains("Document dispatch body."))
        );
        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
    }

    #[cfg(feature = "documents")]
    #[test]
    fn dispatches_pdf_to_combined_path() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("scan.pdf");
        std::fs::write(&file_path, b"%PDF-1.7\nsource bytes\n%%EOF\n").expect("write pdf");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-31T20:03:00Z",
        )
        .expect("ingest pdf");

        assert_eq!(result.record.kind, SourceKind::Pdf);
        assert!(result.asset_path.is_some());
        let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
        assert!(raw.contains("source_kind: pdf"));
        assert!(raw.contains("page_count: "));
        assert!(raw.contains("vision_used: \"false\""));
    }

    #[cfg(not(feature = "documents"))]
    #[test]
    fn office_html_store_as_asset_without_documents_feature() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("page.html");
        std::fs::write(&file_path, b"<html><body>stored only</body></html>").expect("write html");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-31T20:02:00Z",
        )
        .expect("ingest html without documents");

        assert_eq!(result.record.kind, SourceKind::Html);
        assert!(result.asset_path.is_some());
        let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
        assert!(raw.contains("Original artifact stored under"));
    }

    #[cfg(not(feature = "documents"))]
    #[test]
    fn pdf_store_as_asset_without_documents_feature() {
        let temp = tempfile::tempdir().expect("tempdir");
        let file_path = temp.path().join("scan.pdf");
        std::fs::write(&file_path, b"%PDF-1.7\nsource bytes\n%%EOF\n").expect("write pdf");
        let mut store = MemoryWikiStore::default();
        let scope = ScopeIdentity::global();
        let ai_context = no_ai_context();
        let options = ingest_options();

        let result = ingest_path(
            temp.path(),
            &mut store,
            &scope,
            &ai_context,
            &options,
            &file_path,
            "2026-05-31T20:04:00Z",
        )
        .expect("ingest pdf without documents");

        assert_eq!(result.record.kind, SourceKind::Pdf);
        assert!(result.asset_path.is_some());
        let raw = std::fs::read_to_string(temp.path().join(result.raw_path)).expect("raw source");
        assert!(raw.contains("Original artifact stored under"));
    }
}
