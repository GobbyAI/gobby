use std::fmt;
use std::path::PathBuf;
use std::str::FromStr;

use gobby_core::config::AiRouting;
use serde::{Deserialize, Serialize};

use crate::{IngestFileOptions, WikiError};

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
