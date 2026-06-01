use std::path::{Path, PathBuf};

use crate::ingest::{markdown_metadata, markdown_title, path_to_string, single_line};
use crate::sources::SourceRecord;
use crate::{ScopeIdentity, WikiError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptSegment {
    pub start_ms: u64,
    pub end_ms: u64,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionRange {
    pub start_ms: u64,
    pub end_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionOutput {
    pub segments: Vec<TranscriptSegment>,
    pub language: Option<String>,
    pub model: Option<String>,
    pub source_language: Option<String>,
    pub task: Option<String>,
    pub target_language: Option<String>,
    pub translated: bool,
    pub partial: bool,
    pub completed_ranges: Vec<TranscriptionRange>,
    pub missing_ranges: Vec<TranscriptionRange>,
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

    fn translate_to_english(
        &self,
        _request: &TranscriptionRequest<'_>,
        _language_hint: Option<&str>,
    ) -> Result<TranscriptionOutput, WikiError> {
        Err(WikiError::Config {
            detail: "audio translation is not configured".to_string(),
        })
    }

    fn translate_segments(
        &self,
        _segments: &[TranscriptSegment],
        _source_lang: &str,
        _target_lang: &str,
    ) -> Result<Vec<String>, WikiError> {
        Err(WikiError::Config {
            detail: "text translation is not configured".to_string(),
        })
    }
}

#[derive(Debug, Clone, Copy)]
pub struct TranscriptionRequest<'a> {
    pub file_name: &'a str,
    pub mime_type: Option<&'a str>,
    pub asset_path: &'a Path,
    pub bytes: &'a [u8],
}

pub enum TranscriptionEndpoint<'a> {
    Available(Box<dyn TranscriptionClient + 'a>),
    Translating {
        client: Box<dyn TranscriptionClient + 'a>,
        target_lang: Option<String>,
        language_hint: Option<String>,
    },
    Unavailable(TranscriptionDegradation),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TranscriptionMarkdownResult {
    pub path: PathBuf,
    pub degradation: Option<TranscriptionDegradation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TranscriptionMarkdownInput {
    Transcribed(TranscriptionOutput),
    Degraded(TranscriptionDegradation),
}

pub fn write_audio_transcript_markdown(
    vault_root: &Path,
    scope: &ScopeIdentity,
    record: &SourceRecord,
    request: TranscriptionRequest<'_>,
    input: TranscriptionMarkdownInput,
) -> Result<TranscriptionMarkdownResult, WikiError> {
    let (transcription, degradation) = match input {
        TranscriptionMarkdownInput::Transcribed(output) => (Some(output), None),
        TranscriptionMarkdownInput::Degraded(degradation) => (None, Some(degradation)),
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
        transcription.as_ref(),
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
    transcription: Option<&TranscriptionOutput>,
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
        if let Some(source_language) = &output.source_language {
            fields.push((
                "transcription_source_language".to_string(),
                source_language.clone(),
            ));
        }
        if let Some(task) = &output.task {
            fields.push(("transcription_task".to_string(), task.clone()));
        }
        if let Some(target_language) = &output.target_language {
            fields.push((
                "transcription_target_language".to_string(),
                target_language.clone(),
            ));
        }
        fields.push(("translated".to_string(), output.translated.to_string()));
        if output.partial {
            fields.push(("transcription_partial".to_string(), "true".to_string()));
            if !output.missing_ranges.is_empty() {
                fields.push((
                    "transcription_missing_ranges".to_string(),
                    format_ranges_ms(&output.missing_ranges),
                ));
            }
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
        for segment in &output.segments {
            markdown.push('[');
            markdown.push_str(&format_timestamp_ms(segment.start_ms));
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

fn format_timestamp_ms(timestamp_ms: u64) -> String {
    let total_seconds = timestamp_ms / 1_000;
    let hours = total_seconds / 3_600;
    let minutes = (total_seconds % 3_600) / 60;
    let seconds = total_seconds % 60;
    format!("{hours:02}:{minutes:02}:{seconds:02}")
}

fn format_ranges_ms(ranges: &[TranscriptionRange]) -> String {
    ranges
        .iter()
        .map(|range| format!("{}-{}", range.start_ms, range.end_ms))
        .collect::<Vec<_>>()
        .join(",")
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
                        start_ms: 1_000,
                        end_ms: 3_500,
                        text: "First field recording sentence.".to_string(),
                    },
                    TranscriptSegment {
                        start_ms: 4_000,
                        end_ms: 5_250,
                        text: "Second timestamped observation.".to_string(),
                    },
                ],
                language: Some("en".to_string()),
                model: Some("fake-stt".to_string()),
                source_language: Some("en".to_string()),
                task: Some("transcribe".to_string()),
                target_language: None,
                translated: false,
                partial: false,
                completed_ranges: Vec::new(),
                missing_ranges: Vec::new(),
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
            TranscriptionMarkdownInput::Transcribed(
                client
                    .transcribe(&TranscriptionRequest {
                        file_name: "interview.wav",
                        mime_type: Some("audio/wav"),
                        asset_path: &asset_path,
                        bytes: b"audio-bytes",
                    })
                    .expect("transcribe fixture"),
            ),
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
    fn renders_precomputed_output_without_transcribing() {
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
            TranscriptionMarkdownInput::Transcribed(TranscriptionOutput {
                segments: vec![TranscriptSegment {
                    start_ms: 1_000,
                    end_ms: 3_500,
                    text: "Translated field recording sentence.".to_string(),
                }],
                language: Some("en".to_string()),
                model: Some("fake-stt".to_string()),
                source_language: Some("es".to_string()),
                task: Some("translate".to_string()),
                target_language: Some("en".to_string()),
                translated: true,
                partial: true,
                completed_ranges: vec![TranscriptionRange {
                    start_ms: 1_000,
                    end_ms: 3_500,
                }],
                missing_ranges: vec![TranscriptionRange {
                    start_ms: 3_500,
                    end_ms: 7_000,
                }],
            }),
        )
        .expect("write precomputed transcript markdown");

        assert_eq!(client.calls.get(), 0);
        assert!(result.degradation.is_none());

        let markdown =
            std::fs::read_to_string(temp.path().join(&result.path)).expect("transcript markdown");
        assert!(markdown.contains("transcription_status: transcribed"));
        assert!(markdown.contains("transcription_language: en"));
        assert!(markdown.contains("transcription_source_language: es"));
        assert!(markdown.contains("transcription_task: translate"));
        assert!(markdown.contains("transcription_target_language: en"));
        assert!(markdown.contains("translated: true"));
        assert!(markdown.contains("transcription_partial: true"));
        assert!(markdown.contains("transcription_missing_ranges: 3500-7000"));
        assert!(markdown.contains("[00:00:01] Translated field recording sentence."));
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
            TranscriptionMarkdownInput::Degraded(TranscriptionDegradation {
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
