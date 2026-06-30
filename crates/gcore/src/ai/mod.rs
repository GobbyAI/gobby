use std::io::Cursor;
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use reqwest::blocking::{Client, RequestBuilder, Response, multipart};
use reqwest::header::{AUTHORIZATION, RETRY_AFTER};
use serde::Serialize;

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult, TranscriptionResult, VisionResult};
use crate::config::{AiCapability, AiRouting, CapabilityBinding};

pub mod daemon;
pub mod embeddings;
pub mod generation;
pub mod probe;
pub mod text;
pub mod transcription;
pub mod vision;

// Local reasoning models decode long, large-prompt generations at tens of
// tokens per second; a shared 60s budget timed out the biggest codewiki
// prompts (repo overview, top-level subsystems) while vision stays snappy.
const TEXT_GENERATE_TIMEOUT: Duration = Duration::from_secs(300);
const VISION_TIMEOUT: Duration = Duration::from_secs(60);
const EMBEDDINGS_TIMEOUT: Duration = Duration::from_secs(10);
const STT_CHUNK_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_RETRIES: usize = 2;
const BASE_BACKOFF: Duration = Duration::from_millis(250);
const MAX_BACKOFF: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ObservedAiRoute {
    pub route: AiRouting,
    pub fallback: bool,
    pub reason: Option<AiNoticeKind>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum AiNoticeKind {
    AutoFallbackToDirect,
    AutoFallbackToOff,
    NoGenerator,
    GenerationFailed,
}

pub fn effective_route(context: &AiContext, capability: AiCapability) -> AiRouting {
    resolve_route_observed(context, capability).route
}

pub fn resolve_route_observed(context: &AiContext, capability: AiCapability) -> ObservedAiRoute {
    resolve_route_observed_with_probe(context, capability, |capability| {
        probe::probe_daemon_capability(capability).available
    })
}

#[cfg(test)]
fn effective_route_with_probe(
    context: &AiContext,
    capability: AiCapability,
    mut daemon_available: impl FnMut(AiCapability) -> bool,
) -> AiRouting {
    resolve_route_observed_with_probe(context, capability, &mut daemon_available).route
}

pub fn resolve_route_observed_with_probe(
    context: &AiContext,
    capability: AiCapability,
    mut daemon_available: impl FnMut(AiCapability) -> bool,
) -> ObservedAiRoute {
    match context.binding(capability).routing {
        AiRouting::Off => observed(AiRouting::Off, false, None),
        AiRouting::Direct => observed(AiRouting::Direct, false, None),
        AiRouting::Daemon => observed(AiRouting::Daemon, false, None),
        AiRouting::Auto => daemon_route_or_fallback(context, capability, &mut daemon_available),
    }
}

fn daemon_route_or_fallback(
    context: &AiContext,
    capability: AiCapability,
    daemon_available: &mut impl FnMut(AiCapability) -> bool,
) -> ObservedAiRoute {
    // Auto is fail-safe: use daemon only when its status route advertises the
    // capability, then fall back to a configured direct route or Off.
    if daemon_available(capability) {
        observed(AiRouting::Daemon, false, None)
    } else {
        match direct_route_or_off(context, capability) {
            AiRouting::Direct => observed(
                AiRouting::Direct,
                true,
                Some(AiNoticeKind::AutoFallbackToDirect),
            ),
            AiRouting::Off => observed(AiRouting::Off, true, Some(AiNoticeKind::AutoFallbackToOff)),
            route => observed(route, true, None),
        }
    }
}

fn direct_route_or_off(context: &AiContext, capability: AiCapability) -> AiRouting {
    if context
        .binding(capability)
        .api_base
        .as_deref()
        .map(str::trim)
        .is_some_and(|value| !value.is_empty())
    {
        AiRouting::Direct
    } else {
        AiRouting::Off
    }
}

fn observed(route: AiRouting, fallback: bool, reason: Option<AiNoticeKind>) -> ObservedAiRoute {
    ObservedAiRoute {
        route,
        fallback,
        reason,
    }
}

/// Blocking OpenAI-compatible transport skeleton for AI capabilities.
pub struct AiTransport<'a> {
    context: &'a AiContext,
    client: Client,
}

impl<'a> AiTransport<'a> {
    pub fn new(context: &'a AiContext) -> Result<Self, AiError> {
        let client = Client::builder().build().map_err(reqwest_error)?;

        Ok(Self { context, client })
    }

    pub fn post_json<T>(
        &self,
        capability: AiCapability,
        url: &str,
        body: &T,
    ) -> Result<serde_json::Value, AiError>
    where
        T: Serialize,
    {
        let _permit = self.context.limiter.acquire();
        retry_with_backoff(
            || {
                let request = self.build_json_request(capability, url, body)?;
                parse_json_response(request.send().map_err(reqwest_error)?)
            },
            std::thread::sleep,
        )
    }

    pub fn post_multipart(
        &self,
        capability: AiCapability,
        url: &str,
        file_field: &str,
        file_name: &str,
        bytes: Vec<u8>,
        fields: &[(&str, &str)],
    ) -> Result<serde_json::Value, AiError> {
        let _permit = self.context.limiter.acquire();
        let bytes = Bytes::from(bytes);
        retry_with_backoff(
            || {
                let request = self.build_multipart_request(
                    capability,
                    url,
                    file_field,
                    file_name,
                    bytes.clone(),
                    fields,
                )?;
                parse_json_response(request.send().map_err(reqwest_error)?)
            },
            std::thread::sleep,
        )
    }

    pub fn parse_transcription(
        &self,
        value: serde_json::Value,
    ) -> Result<TranscriptionResult, AiError> {
        TranscriptionResult::from_wire_json(value)
    }

    pub fn parse_vision(&self, value: serde_json::Value) -> Result<VisionResult, AiError> {
        VisionResult::from_wire_json(value)
    }

    pub fn parse_text(&self, value: serde_json::Value) -> Result<TextResult, AiError> {
        TextResult::from_wire_json(value)
    }

    pub fn build_json_request<T>(
        &self,
        capability: AiCapability,
        url: &str,
        body: &T,
    ) -> Result<RequestBuilder, AiError>
    where
        T: Serialize,
    {
        let binding = self.context.binding(capability);
        Ok(apply_api_key(
            self.client
                .post(url)
                .timeout(timeout_for(capability))
                .json(body),
            binding,
        ))
    }

    pub fn build_multipart_request(
        &self,
        capability: AiCapability,
        url: &str,
        file_field: &str,
        file_name: &str,
        bytes: Bytes,
        fields: &[(&str, &str)],
    ) -> Result<RequestBuilder, AiError> {
        let binding = self.context.binding(capability);
        let file_len = u64::try_from(bytes.len()).map_err(|_| {
            AiError::parse_failure("multipart payload is too large to send".to_string())
        })?;
        let mut form = multipart::Form::new().part(
            file_field.to_string(),
            multipart::Part::reader_with_length(Cursor::new(bytes), file_len)
                .file_name(file_name.to_string()),
        );

        for (key, value) in fields {
            form = form.text((*key).to_string(), (*value).to_string());
        }

        Ok(apply_api_key(
            self.client
                .post(url)
                .timeout(timeout_for(capability))
                .multipart(form),
            binding,
        ))
    }
}

fn apply_api_key(request: RequestBuilder, binding: &CapabilityBinding) -> RequestBuilder {
    match binding.api_key.as_deref() {
        Some(api_key) => request.header(AUTHORIZATION, format!("Bearer {api_key}")),
        None => request,
    }
}

pub(crate) fn timeout_for(capability: AiCapability) -> Duration {
    match capability {
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => STT_CHUNK_TIMEOUT,
        AiCapability::Embed => EMBEDDINGS_TIMEOUT,
        AiCapability::VisionExtract => VISION_TIMEOUT,
        // Each tool-chat turn is a full reasoning generation; share the
        // text-generation budget rather than the snappy vision/embedding one.
        AiCapability::TextGenerate | AiCapability::ToolChat => TEXT_GENERATE_TIMEOUT,
    }
}

pub fn retry_with_backoff<T>(
    mut operation: impl FnMut() -> Result<T, AiError>,
    mut sleep: impl FnMut(Duration),
) -> Result<T, AiError> {
    for retry_index in 0..=MAX_RETRIES {
        match operation() {
            Ok(value) => return Ok(value),
            Err(error) if retry_index < MAX_RETRIES && is_retryable(&error) => {
                sleep(retry_delay(&error, retry_index));
            }
            Err(error) => return Err(error),
        }
    }

    unreachable!("retry loop always returns");
}

fn is_retryable(error: &AiError) -> bool {
    if error.is_timeout() {
        // A client-side request timeout retried just times out again; the
        // daemon already gives each candidate a full bounded budget.
        return false;
    }
    match error {
        AiError::RateLimited { .. } => true,
        AiError::TransportFailure { status, .. } => status
            .map(|status| status == 408 || status == 429 || status >= 500)
            .unwrap_or(true),
        AiError::HttpStatus { status, .. } => *status == 429 || *status >= 500,
        AiError::CapabilityUnavailable { .. }
        | AiError::NotConfigured { .. }
        | AiError::ParseFailure { .. } => false,
    }
}

fn retry_delay(error: &AiError, retry_index: usize) -> Duration {
    if let Some(retry_after) = error.retry_after() {
        return retry_after.min(MAX_BACKOFF);
    }

    let multiplier = 1_u32 << retry_index.min(16);
    let exponential = BASE_BACKOFF.saturating_mul(multiplier);
    exponential.saturating_add(jitter()).min(MAX_BACKOFF)
}

fn jitter() -> Duration {
    Duration::from_millis(u64::from(rand::random::<u8>() % 50))
}

pub(crate) fn parse_json_response(response: Response) -> Result<serde_json::Value, AiError> {
    let status = response.status();
    let retry_after = response
        .headers()
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(parse_retry_after);
    let body = response.text().map_err(reqwest_error)?;

    if status.as_u16() == 429 {
        return Err(AiError::RateLimited {
            status: Some(429),
            retry_after_ms: retry_after.map(duration_to_ms),
            body: Some(body),
        });
    }

    if status.is_server_error() || status.as_u16() == 408 {
        return Err(AiError::transport_failure(
            Some(status.as_u16()),
            Some(&body),
            format!("HTTP {}", status.as_u16()),
        ));
    }

    if !status.is_success() {
        return Err(AiError::HttpStatus {
            status: status.as_u16(),
            body: Some(body),
        });
    }

    serde_json::from_str(&body).map_err(|error| AiError::parse_failure(error.to_string()))
}

fn parse_retry_after(value: &str) -> Option<Duration> {
    let value = value.trim();
    if let Ok(seconds) = value.parse::<u64>() {
        return Some(Duration::from_secs(seconds).min(MAX_BACKOFF));
    }

    let date = httpdate::parse_http_date(value).ok()?;
    let delay = date
        .duration_since(SystemTime::now())
        .unwrap_or(Duration::ZERO);
    Some(delay.min(MAX_BACKOFF))
}

pub(crate) fn reqwest_error(error: reqwest::Error) -> AiError {
    if error.is_timeout() {
        return AiError::transport_timeout(error.to_string());
    }
    AiError::transport_failure(
        error.status().map(|status| status.as_u16()),
        None,
        error.to_string(),
    )
}

fn duration_to_ms(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

fn chat_completions_url(cfg: &AiContext, capability: AiCapability) -> Result<String, AiError> {
    let binding = cfg.binding(capability);
    let api_base = binding
        .api_base
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| {
            AiError::not_configured(
                Some(capability.as_str().to_string()),
                format!(
                    "{}.api_base is required for direct chat completions",
                    capability.namespace()
                ),
            )
        })?;

    Ok(format!("{}/v1/chat/completions", chat_api_root(api_base)))
}

pub(crate) fn chat_api_root(api_base: &str) -> &str {
    let trimmed = api_base.trim_end_matches('/');
    trimmed.strip_suffix("/v1").unwrap_or(trimmed)
}

pub(crate) fn chat_completion_content(value: &serde_json::Value) -> Result<String, AiError> {
    value
        .get("choices")
        .and_then(serde_json::Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("message"))
        .and_then(|message| message.get("content"))
        .and_then(serde_json::Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| AiError::parse_failure("chat completion response missing message content"))
}

pub(crate) fn chat_completion_model(value: &serde_json::Value) -> Option<String> {
    value
        .get("model")
        .and_then(serde_json::Value::as_str)
        .filter(|model| !model.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_types::AiError;
    use std::time::Duration;

    #[test]
    fn retry_caps_at_two() {
        let mut attempts = 0;
        let result: Result<(), AiError> = retry_with_backoff(
            || {
                attempts += 1;
                Err(AiError::transport_failure(
                    Some(503),
                    Some("busy"),
                    "upstream unavailable",
                ))
            },
            |_| {},
        );

        assert!(matches!(result, Err(AiError::TransportFailure { .. })));
        assert_eq!(attempts, 3);
    }

    #[test]
    fn retry_with_backoff_does_not_retry_a_timeout() {
        let mut attempts = 0;
        let result: Result<(), AiError> = retry_with_backoff(
            || {
                attempts += 1;
                Err(AiError::transport_timeout("request timed out"))
            },
            |_| {},
        );

        assert!(matches!(
            result,
            Err(AiError::TransportFailure { timeout: true, .. })
        ));
        assert_eq!(attempts, 1);
    }

    #[test]
    fn retry_honors_retry_after_before_exponential_backoff() {
        let mut delays = Vec::new();
        let mut attempts = 0;

        let result = retry_with_backoff(
            || {
                attempts += 1;
                if attempts == 1 {
                    Err(AiError::rate_limited(
                        Some(Duration::from_millis(750)),
                        Some("slow down"),
                    ))
                } else {
                    Ok("ok")
                }
            },
            |delay| delays.push(delay),
        );

        assert_eq!(result.unwrap(), "ok");
        assert_eq!(attempts, 2);
        assert_eq!(delays, vec![Duration::from_millis(750)]);
    }

    #[test]
    fn parse_retry_after_accepts_http_dates_and_clamps() {
        let future = SystemTime::now() + MAX_BACKOFF + Duration::from_secs(60);
        let past = SystemTime::UNIX_EPOCH + Duration::from_secs(784_111_777);

        assert_eq!(
            parse_retry_after(&httpdate::fmt_http_date(future)),
            Some(MAX_BACKOFF)
        );
        assert_eq!(
            parse_retry_after(&httpdate::fmt_http_date(past)),
            Some(Duration::ZERO)
        );
        assert_eq!(parse_retry_after("120"), Some(MAX_BACKOFF));
    }

    #[test]
    fn embeddings_use_shorter_timeout_than_generation() {
        assert_eq!(timeout_for(AiCapability::Embed), EMBEDDINGS_TIMEOUT);
        assert!(timeout_for(AiCapability::Embed) < timeout_for(AiCapability::TextGenerate));
        assert!(timeout_for(AiCapability::Embed) < timeout_for(AiCapability::VisionExtract));
    }

    #[test]
    fn text_generation_outlasts_vision_for_local_reasoning_models() {
        assert_eq!(
            timeout_for(AiCapability::TextGenerate),
            TEXT_GENERATE_TIMEOUT
        );
        assert_eq!(timeout_for(AiCapability::VisionExtract), VISION_TIMEOUT);
        assert!(timeout_for(AiCapability::VisionExtract) < timeout_for(AiCapability::TextGenerate));
    }

    #[test]
    fn chat_api_root_trims_trailing_v1_segment() {
        assert_eq!(
            chat_api_root("http://localhost:11434/v1"),
            "http://localhost:11434"
        );
        assert_eq!(
            chat_api_root("http://localhost:11434/custom/v1/"),
            "http://localhost:11434/custom"
        );
        assert_eq!(
            chat_api_root("http://localhost:11434/v10"),
            "http://localhost:11434/v10"
        );
    }

    #[test]
    fn effective_route_auto_falls_through_per_capability() {
        use crate::config::{AiRouting, AiTuning};

        let context = AiContext {
            bindings: crate::ai_context::AiBindings {
                embed: binding(AiRouting::Auto, None),
                audio_transcribe: binding(AiRouting::Auto, Some("http://direct.test")),
                audio_translate: binding(AiRouting::Auto, Some("http://direct.test")),
                vision_extract: binding(AiRouting::Auto, None),
                text_generate: binding(AiRouting::Auto, Some("http://direct.test")),
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: crate::ai_context::AiLimiter::new(1),
            project_id: None,
        };

        assert_eq!(
            effective_route_with_probe(&context, AiCapability::AudioTranscribe, |_| true),
            AiRouting::Daemon
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::AudioTranslate, |_| false),
            AiRouting::Direct
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::VisionExtract, |_| false),
            AiRouting::Off
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::TextGenerate, |_| false),
            AiRouting::Direct
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::Embed, |_| true),
            AiRouting::Daemon
        );
    }

    #[test]
    fn observed_route_reports_auto_fallback_reasons() {
        use crate::config::{AiRouting, AiTuning};

        let context = AiContext {
            bindings: crate::ai_context::AiBindings {
                embed: binding(AiRouting::Auto, None),
                audio_transcribe: binding(AiRouting::Auto, Some("http://direct.test")),
                audio_translate: binding(AiRouting::Auto, Some("http://direct.test")),
                vision_extract: binding(AiRouting::Auto, None),
                text_generate: binding(AiRouting::Auto, Some("http://direct.test")),
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: crate::ai_context::AiLimiter::new(1),
            project_id: None,
        };

        assert_eq!(
            resolve_route_observed_with_probe(&context, AiCapability::TextGenerate, |_| true),
            ObservedAiRoute {
                route: AiRouting::Daemon,
                fallback: false,
                reason: None,
            }
        );
        assert_eq!(
            resolve_route_observed_with_probe(&context, AiCapability::TextGenerate, |_| false),
            ObservedAiRoute {
                route: AiRouting::Direct,
                fallback: true,
                reason: Some(AiNoticeKind::AutoFallbackToDirect),
            }
        );
        assert_eq!(
            resolve_route_observed_with_probe(&context, AiCapability::VisionExtract, |_| false),
            ObservedAiRoute {
                route: AiRouting::Off,
                fallback: true,
                reason: Some(AiNoticeKind::AutoFallbackToOff),
            }
        );
    }

    #[test]
    fn effective_route_explicit_routing_modes_are_forced() {
        use crate::config::{AiRouting, AiTuning};

        let context = AiContext {
            bindings: crate::ai_context::AiBindings {
                embed: binding(AiRouting::Daemon, Some("http://direct.test")),
                audio_transcribe: binding(AiRouting::Daemon, Some("http://direct.test")),
                audio_translate: binding(AiRouting::Auto, Some("http://direct.test")),
                vision_extract: binding(AiRouting::Off, Some("http://direct.test")),
                text_generate: binding(AiRouting::Direct, None),
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: crate::ai_context::AiLimiter::new(1),
            project_id: None,
        };

        assert_eq!(
            effective_route_with_probe(&context, AiCapability::AudioTranscribe, |_| false),
            AiRouting::Daemon
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::TextGenerate, |_| false),
            AiRouting::Direct
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::VisionExtract, |_| true),
            AiRouting::Off
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::Embed, |_| true),
            AiRouting::Daemon
        );
        assert_eq!(
            resolve_route_observed_with_probe(&context, AiCapability::TextGenerate, |_| false),
            ObservedAiRoute {
                route: AiRouting::Direct,
                fallback: false,
                reason: None,
            }
        );
        assert_eq!(
            resolve_route_observed_with_probe(&context, AiCapability::AudioTranscribe, |_| false),
            ObservedAiRoute {
                route: AiRouting::Daemon,
                fallback: false,
                reason: None,
            }
        );
    }

    #[test]
    fn auto_uses_explicit_direct_config_when_daemon_unavailable() {
        use crate::config::{AiRouting, AiTuning};

        let context = AiContext {
            bindings: crate::ai_context::AiBindings {
                embed: binding(AiRouting::Auto, None),
                audio_transcribe: binding(AiRouting::Auto, None),
                audio_translate: binding(AiRouting::Auto, None),
                vision_extract: binding(AiRouting::Auto, None),
                text_generate: binding(AiRouting::Auto, Some("http://direct.test")),
            },
            tuning: AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: crate::ai_context::AiLimiter::new(1),
            project_id: None,
        };

        assert_eq!(
            context
                .binding(AiCapability::TextGenerate)
                .api_base
                .as_deref(),
            Some("http://direct.test")
        );
        assert_eq!(
            effective_route_with_probe(&context, AiCapability::TextGenerate, |_| false),
            AiRouting::Direct
        );
    }

    fn binding(routing: AiRouting, api_base: Option<&str>) -> CapabilityBinding {
        CapabilityBinding {
            routing,
            transport: None,
            api_base: api_base.map(str::to_string),
            api_key: None,
            model: None,
            provider: None,
            task: None,
            language: None,
            target_lang: None,
            profile: None,
            candidates: None,
            reasoning_effort: None,
            verify_profile: None,
            verify_model: None,
            verify_api_key: None,
        }
    }
}
