use std::path::{Path, PathBuf};

use crate::ingest::{
    IngestResult, index_after_ingest, markdown_metadata, markdown_title, path_to_string,
    write_asset, write_raw_markdown,
};
use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};
use crate::store::WikiIndexStore;
use crate::transcribe::{
    TranscriptionDegradation, TranscriptionEndpoint, TranscriptionRequest,
    write_audio_transcript_markdown,
};
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioSnapshot {
    pub location: String,
    pub file_name: String,
    pub fetched_at: String,
    pub bytes: Vec<u8>,
    pub mime_type: Option<String>,
    pub duration_seconds: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AudioIngestResult {
    pub record: crate::sources::SourceRecord,
    pub raw_path: PathBuf,
    pub asset_path: PathBuf,
    pub transcript_path: PathBuf,
    pub transcription_degradation: Option<TranscriptionDegradation>,
}

pub fn ingest_audio(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
) -> Result<AudioIngestResult, WikiError> {
    ingest_audio_with_transcription(
        vault_root,
        store,
        scope,
        snapshot,
        TranscriptionEndpoint::Unavailable(TranscriptionDegradation {
            reason: "missing_endpoint".to_string(),
            fallback:
                "Keep raw audio assets and require supplied transcripts; skip daemon transcription."
                    .to_string(),
        }),
    )
}

pub fn ingest_audio_with_transcription(
    vault_root: &Path,
    store: &mut impl WikiIndexStore,
    scope: ScopeIdentity,
    snapshot: AudioSnapshot,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<AudioIngestResult, WikiError> {
    let title = markdown_title(&snapshot.file_name);
    let content_hash = gobby_core::indexing::content_hash(&snapshot.bytes);
    let draft = SourceDraft {
        location: snapshot.location.clone(),
        kind: SourceKind::Audio,
        fetched_at: snapshot.fetched_at.clone(),
        content: Vec::new(),
        title: Some(title),
        citation: Some(snapshot.location.clone()),
        license: None,
        ingestion_method: IngestionMethod::Manual,
        compile_status: CompileStatus::Pending,
    };
    let record = SourceManifest::register_with_content_hash(vault_root, draft, content_hash)?;
    let asset_path = write_asset(vault_root, &record, &snapshot.file_name, &snapshot.bytes)?;
    let raw_markdown = render_raw_audio_markdown(&snapshot, &record.content_hash, &asset_path);
    let raw_path = write_raw_markdown(vault_root, &record, &raw_markdown)?;
    let transcript = write_audio_transcript_markdown(
        vault_root,
        &scope,
        &record,
        TranscriptionRequest {
            file_name: &snapshot.file_name,
            mime_type: snapshot.mime_type.as_deref(),
            asset_path: &asset_path,
            bytes: &snapshot.bytes,
        },
        endpoint,
    )?;
    index_after_ingest(vault_root, store)?;

    Ok(AudioIngestResult {
        record,
        raw_path,
        asset_path,
        transcript_path: transcript.path,
        transcription_degradation: transcript.degradation,
    })
}

impl From<AudioIngestResult> for IngestResult {
    fn from(result: AudioIngestResult) -> Self {
        Self {
            record: result.record,
            raw_path: result.raw_path,
            asset_path: Some(result.asset_path),
        }
    }
}

fn render_raw_audio_markdown(
    snapshot: &AudioSnapshot,
    source_hash: &str,
    asset_path: &Path,
) -> String {
    let asset_path = path_to_string(asset_path);
    let mut fields = vec![
        ("source_kind", "audio".to_string()),
        ("source_location", snapshot.location.clone()),
        ("fetched_at", snapshot.fetched_at.clone()),
        ("source_hash", source_hash.to_string()),
        ("source_asset", asset_path.clone()),
    ];
    if let Some(mime_type) = &snapshot.mime_type {
        fields.push(("audio_mime_type", mime_type.clone()));
    }
    if let Some(duration_seconds) = snapshot.duration_seconds {
        fields.push(("audio_duration_seconds", duration_seconds.to_string()));
    }

    let mut markdown = markdown_metadata(&fields);
    markdown.push_str("# ");
    markdown.push_str(&markdown_title(&snapshot.file_name));
    markdown.push_str("\n\n");
    markdown.push_str("Original audio stored under `");
    markdown.push_str(&asset_path);
    markdown.push_str("`.\n");
    markdown
}

#[cfg(test)]
mod tests {
    use gobby_core::indexing::content_hash;

    use super::*;
    use crate::sources::{SourceKind, SourceManifest};
    use crate::store::{MemoryWikiStore, WikiDocumentKind};
    use crate::transcribe::{
        TranscriptSegment, TranscriptionClient, TranscriptionOutput, TranscriptionRequest,
    };

    fn sample_snapshot() -> AudioSnapshot {
        AudioSnapshot {
            location: "/tmp/interview.wav".to_string(),
            file_name: "interview.wav".to_string(),
            fetched_at: "2026-05-29T21:15:00Z".to_string(),
            bytes: b"RIFF....WAVEaudio-bytes".to_vec(),
            mime_type: Some("audio/wav".to_string()),
            duration_seconds: Some(12),
        }
    }

    struct FakeTranscriptionClient;

    impl TranscriptionClient for FakeTranscriptionClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            Ok(TranscriptionOutput {
                segments: vec![TranscriptSegment {
                    timestamp: "00:00:02".to_string(),
                    text: "Scope searchable hydrophone transcript phrase.".to_string(),
                }],
                language: Some("en".to_string()),
                model: Some("fake-stt".to_string()),
            })
        }
    }

    #[test]
    fn stores_original_audio() {
        let temp = tempfile::tempdir().expect("tempdir");
        let snapshot = sample_snapshot();
        let expected_hash = content_hash(&snapshot.bytes);
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio(
            temp.path(),
            &mut store,
            ScopeIdentity::topic("field-work"),
            snapshot.clone(),
        )
        .expect("ingest audio");

        assert_eq!(
            result.asset_path.parent(),
            Some(PathBuf::from("raw/assets").as_path())
        );
        assert_eq!(
            std::fs::read(temp.path().join(&result.asset_path)).expect("asset bytes"),
            snapshot.bytes
        );
        let raw =
            std::fs::read_to_string(temp.path().join(&result.raw_path)).expect("raw markdown");
        assert!(raw.contains("source_kind: audio"));
        assert!(raw.contains("source_asset: raw/assets/"));
        assert!(raw.contains("audio_mime_type: audio/wav"));
        assert!(raw.contains("audio_duration_seconds: 12"));

        let manifest = SourceManifest::read(temp.path()).expect("read source manifest");
        assert_eq!(manifest.entries.len(), 1);
        assert_eq!(manifest.entries[0].kind, SourceKind::Audio);
        assert_eq!(manifest.entries[0].content_hash, expected_hash);
    }

    #[test]
    fn transcript_chunks_are_scope_searchable() {
        let temp = tempfile::tempdir().expect("tempdir");
        let mut store = MemoryWikiStore::default();

        let result = ingest_audio_with_transcription(
            temp.path(),
            &mut store,
            ScopeIdentity::project("project-123"),
            sample_snapshot(),
            TranscriptionEndpoint::Available(&FakeTranscriptionClient),
        )
        .expect("ingest audio with transcript");

        let document = store
            .documents
            .get(&result.transcript_path)
            .expect("transcript document indexed");
        assert_eq!(document.kind, WikiDocumentKind::SourceNote);
        assert!(document.body.contains("scope_kind: project"));
        assert!(document.body.contains("scope_id: project-123"));
        assert!(
            document
                .body
                .contains("Scope searchable hydrophone transcript phrase.")
        );
        assert!(store.sources.contains_key(&result.transcript_path));
        let chunks = store
            .chunks
            .get(&result.transcript_path)
            .expect("transcript chunks indexed");
        assert!(chunks.iter().any(|chunk| {
            chunk
                .content
                .contains("Scope searchable hydrophone transcript phrase.")
        }));
    }
}
