use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// A transcription segment normalized to integer milliseconds.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TranscriptionSegment {
    pub start_ms: u64,
    pub end_ms: u64,
    pub text: String,
}

/// Speech-to-text or speech-translation output shared across AI transports.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TranscriptionResult {
    pub text: String,
    pub segments: Vec<TranscriptionSegment>,
    pub source_language: Option<String>,
    pub language: Option<String>,
    pub model: Option<String>,
    pub task: Option<String>,
    pub target_language: Option<String>,
    pub translated: bool,
}

impl TranscriptionResult {
    pub fn from_wire_json(value: serde_json::Value) -> Result<Self, AiError> {
        serde_json::from_value::<WireTranscriptionResult>(value)
            .map_err(|error| AiError::parse_failure(error.to_string()))
            .and_then(TryFrom::try_from)
    }
}

/// Vision extraction output with OCR kept separately for retrieval indexing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VisionResult {
    pub description: String,
    pub ocr_text: Option<String>,
    pub model: Option<String>,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}

impl VisionResult {
    pub fn from_wire_json(value: serde_json::Value) -> Result<Self, AiError> {
        serde_json::from_value::<Self>(value)
            .map_err(|error| AiError::parse_failure(error.to_string()))
    }
}

/// Text-generation output shared across AI transports.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TextResult {
    pub text: String,
    pub model: Option<String>,
    /// Provider-specific metadata preserved for diagnostics and callers that
    /// need backend details without depending on a transport-specific schema.
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}

impl TextResult {
    pub fn from_wire_json(value: serde_json::Value) -> Result<Self, AiError> {
        serde_json::from_value::<Self>(value)
            .map_err(|error| AiError::parse_failure(error.to_string()))
    }
}

/// Transport-neutral AI error type.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AiError {
    CapabilityUnavailable {
        capability: String,
        message: String,
    },
    NotConfigured {
        capability: Option<String>,
        message: String,
    },
    TransportFailure {
        status: Option<u16>,
        body: Option<String>,
        source: String,
    },
    RateLimited {
        status: Option<u16>,
        retry_after_ms: Option<u64>,
        body: Option<String>,
    },
    HttpStatus {
        status: u16,
        body: Option<String>,
    },
    ParseFailure {
        source: String,
    },
}

impl AiError {
    pub fn capability_unavailable(
        capability: impl Into<String>,
        message: impl Into<String>,
    ) -> Self {
        Self::CapabilityUnavailable {
            capability: capability.into(),
            message: message.into(),
        }
    }

    pub fn not_configured(capability: Option<String>, message: impl Into<String>) -> Self {
        Self::NotConfigured {
            capability,
            message: message.into(),
        }
    }

    pub fn transport_failure(
        status: Option<u16>,
        body: Option<&str>,
        source: impl Into<String>,
    ) -> Self {
        Self::TransportFailure {
            status,
            body: body.map(str::to_string),
            source: source.into(),
        }
    }

    pub fn rate_limited(retry_after: Option<Duration>, body: Option<&str>) -> Self {
        Self::RateLimited {
            status: Some(429),
            retry_after_ms: retry_after.map(duration_to_ms),
            body: body.map(str::to_string),
        }
    }

    pub fn parse_failure(source: impl Into<String>) -> Self {
        Self::ParseFailure {
            source: source.into(),
        }
    }

    pub fn status(&self) -> Option<u16> {
        match self {
            Self::TransportFailure { status, .. } | Self::RateLimited { status, .. } => *status,
            Self::HttpStatus { status, .. } => Some(*status),
            Self::CapabilityUnavailable { .. }
            | Self::NotConfigured { .. }
            | Self::ParseFailure { .. } => None,
        }
    }

    pub fn retry_after(&self) -> Option<Duration> {
        match self {
            Self::RateLimited {
                retry_after_ms: Some(ms),
                ..
            } => Some(Duration::from_millis(*ms)),
            _ => None,
        }
    }
}

impl fmt::Display for AiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CapabilityUnavailable {
                capability,
                message,
            } => write!(f, "AI capability `{capability}` is unavailable: {message}"),
            Self::NotConfigured { message, .. } => {
                write!(f, "AI capability is not configured: {message}")
            }
            Self::TransportFailure { source, .. } => write!(f, "AI transport failed: {source}"),
            Self::RateLimited { .. } => write!(f, "AI request was rate limited"),
            Self::HttpStatus { status, .. } => write!(f, "AI endpoint returned HTTP {status}"),
            Self::ParseFailure { source } => write!(f, "AI response parse failed: {source}"),
        }
    }
}

impl std::error::Error for AiError {}

#[derive(Debug, Deserialize)]
struct WireTranscriptionResult {
    #[serde(default)]
    text: String,
    #[serde(default)]
    segments: Vec<WireTranscriptionSegment>,
    #[serde(default)]
    source_language: Option<String>,
    #[serde(default)]
    language: Option<String>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    task: Option<String>,
    #[serde(default, alias = "target_lang")]
    target_language: Option<String>,
    #[serde(default)]
    translated: bool,
}

#[derive(Debug, Deserialize)]
struct WireTranscriptionSegment {
    start: f64,
    end: f64,
    text: String,
}

impl TryFrom<WireTranscriptionResult> for TranscriptionResult {
    type Error = AiError;

    fn try_from(value: WireTranscriptionResult) -> Result<Self, Self::Error> {
        let segments = value
            .segments
            .into_iter()
            .map(TranscriptionSegment::try_from)
            .collect::<Result<Vec<_>, _>>()?;

        Ok(Self {
            text: value.text,
            segments,
            source_language: value.source_language,
            language: value.language,
            model: value.model,
            task: value.task,
            target_language: value.target_language,
            translated: value.translated,
        })
    }
}

impl TryFrom<WireTranscriptionSegment> for TranscriptionSegment {
    type Error = AiError;

    fn try_from(value: WireTranscriptionSegment) -> Result<Self, Self::Error> {
        let start_ms = seconds_to_ms(value.start)?;
        let end_ms = seconds_to_ms(value.end)?;
        if end_ms < start_ms {
            return Err(AiError::parse_failure(
                "transcription segment end time precedes start time",
            ));
        }
        Ok(Self {
            start_ms,
            end_ms,
            text: value.text,
        })
    }
}

fn seconds_to_ms(seconds: f64) -> Result<u64, AiError> {
    if !seconds.is_finite() || seconds.is_sign_negative() {
        return Err(AiError::parse_failure(
            "transcription segment times must be finite, non-negative seconds",
        ));
    }
    let milliseconds = (seconds * 1000.0).round();
    if !milliseconds.is_finite() || milliseconds >= u64::MAX as f64 {
        return Err(AiError::parse_failure(
            "transcription segment times are too large to represent in milliseconds",
        ));
    }
    Ok(milliseconds as u64)
}

fn duration_to_ms(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ai_error_is_transport_neutral() {
        let error = AiError::transport_failure(Some(503), Some("try later"), "connection reset");

        assert_eq!(error.status(), Some(503));
        let rendered = format!("{error:?}");
        assert!(!rendered.contains("reqwest::"));
        assert!(!rendered.contains("ureq::"));
    }

    #[test]
    fn transcription_wire_seconds_round_to_integer_milliseconds() {
        let json = serde_json::json!({
            "text": "hello world",
            "language": "en",
            "model": "faster-whisper-small",
            "task": "transcribe",
            "segments": [
                { "start": 0.1246, "end": 1.4999, "text": "hello" },
                { "start": 1.5, "end": 2.0, "text": " world" }
            ]
        });

        let result = TranscriptionResult::from_wire_json(json).unwrap();

        assert_eq!(
            result.segments,
            vec![
                TranscriptionSegment {
                    start_ms: 125,
                    end_ms: 1500,
                    text: "hello".to_string(),
                },
                TranscriptionSegment {
                    start_ms: 1500,
                    end_ms: 2000,
                    text: " world".to_string(),
                },
            ]
        );
        assert_eq!(result.language.as_deref(), Some("en"));
        assert!(!result.translated);
    }

    #[test]
    fn transcription_wire_seconds_reject_overflowing_milliseconds() {
        let json = serde_json::json!({
            "text": "too long",
            "segments": [
                { "start": 0.0, "end": f64::MAX, "text": "too long" }
            ]
        });

        let error = TranscriptionResult::from_wire_json(json).expect_err("overflow rejected");

        assert!(error.to_string().contains("too large"));
    }

    #[test]
    fn transcription_wire_seconds_reject_reversed_segments() {
        let json = serde_json::json!({
            "text": "bad segment",
            "segments": [
                { "start": 2.0, "end": 1.0, "text": "backwards" }
            ]
        });

        let error = TranscriptionResult::from_wire_json(json).expect_err("segment rejected");

        assert!(matches!(error, AiError::ParseFailure { .. }));
        assert!(error.to_string().contains("precedes start"));
    }
}
