use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::blocking::{Client, RequestBuilder, Response, multipart};
use reqwest::header::{AUTHORIZATION, RETRY_AFTER};
use serde::Serialize;

use crate::ai_context::AiContext;
use crate::ai_types::{AiError, TextResult, TranscriptionResult, VisionResult};
use crate::config::{AiCapability, CapabilityBinding};

const TEXT_VISION_TIMEOUT: Duration = Duration::from_secs(60);
const STT_CHUNK_TIMEOUT: Duration = Duration::from_secs(120);
const MAX_RETRIES: usize = 2;
const BASE_BACKOFF: Duration = Duration::from_millis(250);
const MAX_BACKOFF: Duration = Duration::from_secs(30);

/// Blocking OpenAI-compatible transport skeleton for AI capabilities.
pub struct AiTransport<'a> {
    context: &'a AiContext,
    client: Client,
}

impl<'a> AiTransport<'a> {
    pub fn new(context: &'a AiContext) -> Result<Self, AiError> {
        let client = Client::builder()
            .timeout(TEXT_VISION_TIMEOUT)
            .build()
            .map_err(reqwest_error)?;

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
        bytes: Vec<u8>,
        fields: &[(&str, &str)],
    ) -> Result<RequestBuilder, AiError> {
        let binding = self.context.binding(capability);
        let mut form = multipart::Form::new().part(
            file_field.to_string(),
            multipart::Part::bytes(bytes).file_name(file_name.to_string()),
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

fn timeout_for(capability: AiCapability) -> Duration {
    match capability {
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => STT_CHUNK_TIMEOUT,
        AiCapability::Embed | AiCapability::VisionExtract | AiCapability::TextGenerate => {
            TEXT_VISION_TIMEOUT
        }
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
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.subsec_nanos())
        .unwrap_or(0);
    Duration::from_millis(u64::from(nanos % 50))
}

fn parse_json_response(response: Response) -> Result<serde_json::Value, AiError> {
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
    value
        .trim()
        .parse::<u64>()
        .ok()
        .map(Duration::from_secs)
        .map(|duration| duration.min(MAX_BACKOFF))
}

fn reqwest_error(error: reqwest::Error) -> AiError {
    AiError::transport_failure(
        error.status().map(|status| status.as_u16()),
        None,
        error.to_string(),
    )
}

fn duration_to_ms(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

#[cfg(feature = "local_backend")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalBackendProbe {
    pub url: String,
    pub status: u16,
    pub body: Option<String>,
}

#[cfg(feature = "local_backend")]
pub fn probe_local_backend(api_base: &str) -> Result<LocalBackendProbe, AiError> {
    let url = format!("{}/models", api_base.trim_end_matches('/'));
    match ureq::get(&url).timeout(Duration::from_secs(2)).call() {
        Ok(response) => Ok(LocalBackendProbe {
            url,
            status: response.status(),
            body: response.into_string().ok(),
        }),
        Err(ureq::Error::Status(status, response)) => Err(AiError::transport_failure(
            Some(status),
            response.into_string().ok().as_deref(),
            format!("HTTP {status}"),
        )),
        Err(error) => Err(AiError::transport_failure(None, None, error.to_string())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ai_types::AiError;
    use std::time::Duration;

    #[test]
    fn retry_caps_at_two() {
        let mut attempts = 0;
        let result = retry_with_backoff(
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
}
