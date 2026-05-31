use std::path::{Path, PathBuf};

use crate::ingest::{markdown_metadata, markdown_title, path_to_string, single_line};
use crate::sources::SourceRecord;
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptSegment {
    pub timestamp: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionOutput {
    pub segments: Vec<TranscriptSegment>,
    pub language: Option<String>,
    pub model: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionDegradation {
    pub reason: String,
    pub fallback: String,
}

pub trait TranscriptionClient {
    fn transcribe(
        &self,
        request: &TranscriptionRequest<'_>,
    ) -> Result<TranscriptionOutput, WikiError>;
}

#[derive(Debug, Clone, Copy)]
pub struct TranscriptionRequest<'a> {
    pub file_name: &'a str,
    pub mime_type: Option<&'a str>,
    pub asset_path: &'a Path,
    pub bytes: &'a [u8],
}

pub enum TranscriptionEndpoint<'a> {
    Available(&'a dyn TranscriptionClient),
    Unavailable(TranscriptionDegradation),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionMarkdownResult {
    pub path: PathBuf,
    pub degradation: Option<TranscriptionDegradation>,
}

pub fn write_audio_transcript_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: TranscriptionRequest<'_>,
    endpoint: TranscriptionEndpoint<'_>,
) -> Result<TranscriptionMarkdownResult, WikiError> {
    let (transcription, degradation) = match endpoint {
        TranscriptionEndpoint::Available(client) => match client.transcribe(&request) {
            Ok(output) => (Some(output), None),
            Err(error) => (
                None,
                Some(TranscriptionDegradation {
                    reason: "transcription_error".to_string(),
                    fallback: format!(
                        "Transcription failed: {error}; keep raw audio assets and require supplied transcripts."
                    ),
                }),
            ),
        },
        TranscriptionEndpoint::Unavailable(degradation) => (None, Some(degradation)),
    };

    let relative_path = derived_markdown_path(record);
    let path = vault_root.join(&relative_path);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| WikiError::Io {
            action: "create audio transcript directory",
            path: Some(parent.to_path_buf()),
            source: error,
        })?;
    }

    let markdown = render_audio_transcript_markdown(
        scope,
        record,
        request,
        transcription,
        degradation.as_ref(),
    );
    std::fs::write(&path, markdown).map_err(|error| WikiError::Io {
        action: "write audio transcript markdown",
        path: Some(path),
        source: error,
    })?;

    Ok(TranscriptionMarkdownResult {
        path: relative_path,
        degradation,
    })
}

fn derived_markdown_path(record: &SourceRecord) -> PathBuf {
    PathBuf::from("wiki")
        .join("sources")
        .join(format!("{}.md", record.id))
}

fn render_audio_transcript_markdown(
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: TranscriptionRequest<'_>,
    transcription: Option<TranscriptionOutput>,
    degradation: Option<&TranscriptionDegradation>,
) -> String {
    let title = markdown_title(request.file_name);
    let asset_path = path_to_string(request.asset_path);
    let raw_path = format!("raw/{}.md", record.id);
    let mut fields = vec![
        ("title".to_string(), title.clone()),
        ("source_kind".to_string(), "audio".to_string()),
        ("source_location".to_string(), record.location.clone()),
        ("source_hash".to_string(), record.content_hash.clone()),
        ("source_asset".to_string(), asset_path.clone()),
        ("source_raw".to_string(), raw_path.clone()),
        ("fetched_at".to_string(), record.fetched_at.clone()),
        ("scope_kind".to_string(), scope.kind.as_str().to_string()),
        ("scope_id".to_string(), scope.id.clone()),
        (
            "transcription_status".to_string(),
            if transcription.is_some() {
                "transcribed".to_string()
            } else {
                "unavailable".to_string()
            },
        ),
        ("audio_bytes".to_string(), request.bytes.len().to_string()),
    ];
    if let Some(mime_type) = request.mime_type {
        fields.push(("audio_mime_type".to_string(), mime_type.to_string()));
    }
    if let Some(output) = &transcription {
        if let Some(language) = &output.language {
            fields.push(("transcription_language".to_string(), language.clone()));
        }
        if let Some(model) = &output.model {
            fields.push(("transcription_model".to_string(), model.clone()));
        }
    }
    if let Some(degradation) = degradation {
        fields.push((
            "transcription_degradation".to_string(),
            degradation.reason.clone(),
        ));
    }

    let mut markdown = {
        let field_refs = fields
            .iter()
            .map(|(key, value)| (key.as_str(), value.clone()))
            .collect::<Vec<_>>();
        markdown_metadata(&field_refs)
    };
    markdown.push_str("# ");
    markdown.push_str(&title);
    markdown.push_str("\n\n");
    markdown.push_str("Original audio: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n\n");
    markdown.push_str("Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n\n");

    if let Some(output) = transcription {
        markdown.push_str("## Transcript\n\n");
        for segment in output.segments {
            markdown.push('[');
            markdown.push_str(&single_line(&segment.timestamp));
            markdown.push_str("] ");
            markdown.push_str(&single_line(&segment.text));
            markdown.push('\n');
        }
        markdown.push('\n');
    } else if let Some(degradation) = degradation {
        markdown.push_str("## Transcription Unavailable\n\n");
        markdown.push_str(&single_line(&degradation.reason));
        markdown.push_str(": ");
        markdown.push_str(&single_line(&degradation.fallback));
        markdown.push_str("\n\n");
    }

    markdown.push_str("## Source References\n\n");
    markdown.push_str("- Raw source: `");
    markdown.push_str(&raw_path);
    markdown.push_str("`\n");
    markdown.push_str("- Original audio: `");
    markdown.push_str(&asset_path);
    markdown.push_str("`\n");
    if let Some(citation) = &record.citation {
        markdown.push_str("- Citation: ");
        markdown.push_str(&single_line(citation));
        markdown.push('\n');
    }
    markdown
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::*;
    use crate::sources::{CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest};

    struct FakeTranscriptionClient {
        calls: Cell<usize>,
    }

    impl TranscriptionClient for FakeTranscriptionClient {
        fn transcribe(
            &self,
            _request: &TranscriptionRequest<'_>,
        ) -> Result<TranscriptionOutput, WikiError> {
            self.calls.set(self.calls.get() + 1);
            Ok(TranscriptionOutput {
                segments: vec![
                    TranscriptSegment {
                        timestamp: "00:00:01".to_string(),
                        text: "First field recording sentence.".to_string(),
                    },
                    TranscriptSegment {
                        timestamp: "00:00:04".to_string(),
                        text: "Second timestamped observation.".to_string(),
                    },
                ],
                language: Some("en".to_string()),
                model: Some("fake-stt".to_string()),
            })
        }
    }

    fn record_for(temp: &Path) -> SourceRecord {
        SourceManifest::register(
            temp,
            SourceDraft {
                location: "/tmp/interview.wav".to_string(),
                kind: SourceKind::Audio,
                fetched_at: "2026-05-29T21:00:00Z".to_string(),
                content: b"audio-bytes".to_vec(),
                title: Some("interview.wav".to_string()),
                citation: Some("/tmp/interview.wav".to_string()),
                license: None,
                ingestion_method: IngestionMethod::Manual,
                compile_status: CompileStatus::Pending,
            },
        )
        .expect("register audio source")
    }

    #[test]
    fn writes_timestamped_transcript() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let asset_path = PathBuf::from("raw/assets/interview.wav");
        let client = FakeTranscriptionClient {
            calls: Cell::new(0),
        };

        let result = write_audio_transcript_markdown(
            temp.path(),
            &ScopeIdentity::topic("field-work"),
            &record,
            TranscriptionRequest {
                file_name: "interview.wav",
                mime_type: Some("audio/wav"),
                asset_path: &asset_path,
                bytes: b"audio-bytes",
            },
            TranscriptionEndpoint::Available(&client),
        )
        .expect("write transcript markdown");

        assert_eq!(client.calls.get(), 1);
        assert_eq!(
            result.path,
            PathBuf::from("wiki/sources").join(format!("{}.md", record.id))
        );
        assert!(result.degradation.is_none());

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("transcript markdown");
        assert!(markdown.contains("source_kind: audio"));
        assert!(markdown.contains("scope_kind: topic"));
        assert!(markdown.contains("scope_id: field-work"));
        assert!(markdown.contains("transcription_status: transcribed"));
        assert!(markdown.contains("[00:00:01] First field recording sentence."));
        assert!(markdown.contains("[00:00:04] Second timestamped observation."));
        assert!(markdown.contains("Original audio: `raw/assets/interview.wav`"));
        assert!(markdown.contains("Raw source: `raw/"));
        assert!(markdown.contains("## Source References"));
    }

    #[test]
    fn missing_transcription_degrades() {
        let temp = tempfile::tempdir().expect("tempdir");
        let record = record_for(temp.path());
        let asset_path = PathBuf::from("raw/assets/interview.wav");
        std::fs::create_dir_all(temp.path().join("raw/assets")).expect("asset dir");
        std::fs::write(temp.path().join(&asset_path), b"audio-bytes").expect("asset");

        let result = write_audio_transcript_markdown(
            temp.path(),
            &ScopeIdentity::project("project-123"),
            &record,
            TranscriptionRequest {
                file_name: "interview.wav",
                mime_type: Some("audio/wav"),
                asset_path: &asset_path,
                bytes: b"audio-bytes",
            },
            TranscriptionEndpoint::Unavailable(TranscriptionDegradation {
                reason: "missing_endpoint".to_string(),
                fallback: "Keep raw audio assets and require supplied transcripts.".to_string(),
            }),
        )
        .expect("write degraded transcript markdown");

        let degradation = result.degradation.expect("degradation");
        assert_eq!(degradation.reason, "missing_endpoint");
        assert_eq!(
            std::fs::read(temp.path().join(&asset_path)).expect("asset remains"),
            b"audio-bytes"
        );

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("transcript markdown");
        assert!(markdown.contains("transcription_status: unavailable"));
        assert!(markdown.contains("transcription_degradation: missing_endpoint"));
        assert!(markdown.contains("Keep raw audio assets"));
        assert!(markdown.contains("Original audio: `raw/assets/interview.wav`"));
    }
}
