use std::time::Duration;

use serde_json::Value;

use crate::config::AiCapability;
use crate::daemon_url::daemon_url;

const PROBE_TIMEOUT: Duration = Duration::from_millis(750);

const PROBED_CAPABILITIES: [AiCapability; 5] = [
    AiCapability::Embed,
    AiCapability::AudioTranscribe,
    AiCapability::AudioTranslate,
    AiCapability::VisionExtract,
    AiCapability::TextGenerate,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CapabilityStatusRoute {
    pub method: &'static str,
    pub path: &'static str,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CapabilityDegradationReason {
    MissingStatusRoute,
    MissingEndpoint,
    Unauthorized,
    UnexpectedStatus,
    Unreachable,
    NotAdvertised,
    InvalidStatusBody,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapabilityDegradation {
    pub capability: AiCapability,
    pub reason: CapabilityDegradationReason,
    pub message: String,
    pub http_status: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapabilityAvailability {
    pub capability: AiCapability,
    pub available: bool,
    pub status_route: Option<CapabilityStatusRoute>,
    pub degradation: Option<CapabilityDegradation>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CapabilityProbeReport {
    pub base_url: String,
    pub capabilities: Vec<CapabilityAvailability>,
}

impl CapabilityProbeReport {
    pub fn availability(&self, capability: AiCapability) -> Option<&CapabilityAvailability> {
        self.capabilities
            .iter()
            .find(|availability| availability.capability == capability)
    }
}

pub fn capability_status_route(capability: AiCapability) -> Option<CapabilityStatusRoute> {
    let path = match capability {
        AiCapability::AudioTranscribe | AiCapability::AudioTranslate => "/api/voice/status",
        AiCapability::VisionExtract => "/api/llm/vision/status",
        AiCapability::TextGenerate => "/api/llm/status",
        // Embeddings currently have direct local/OpenAI-compatible probes, but
        // the daemon does not expose a stable HTTP status route for them.
        AiCapability::Embed => return None,
    };

    Some(CapabilityStatusRoute {
        method: "GET",
        path,
    })
}

pub fn probe_daemon_capability(capability: AiCapability) -> CapabilityAvailability {
    probe_daemon_capability_at(&daemon_url(), capability)
}

pub fn probe_daemon_capability_at(
    base_url: &str,
    capability: AiCapability,
) -> CapabilityAvailability {
    probe_daemon_capability_with(base_url, capability, &UreqProbeTransport)
}

pub fn probe_daemon_capabilities() -> CapabilityProbeReport {
    probe_daemon_capabilities_at(&daemon_url())
}

pub fn probe_daemon_capabilities_at(base_url: &str) -> CapabilityProbeReport {
    probe_daemon_capabilities_with(base_url, &UreqProbeTransport)
}

fn probe_daemon_capabilities_with(
    base_url: &str,
    transport: &impl DaemonProbeTransport,
) -> CapabilityProbeReport {
    CapabilityProbeReport {
        base_url: base_url.to_string(),
        capabilities: PROBED_CAPABILITIES
            .into_iter()
            .map(|capability| probe_daemon_capability_with(base_url, capability, transport))
            .collect(),
    }
}

fn probe_daemon_capability_with(
    base_url: &str,
    capability: AiCapability,
    transport: &impl DaemonProbeTransport,
) -> CapabilityAvailability {
    let Some(route) = capability_status_route(capability) else {
        return unavailable(
            capability,
            None,
            CapabilityDegradationReason::MissingStatusRoute,
            "daemon status route is not defined for this capability",
            None,
        );
    };

    match transport.status(base_url, route.method, route.path) {
        ProbeObservation::Http { status, body } if (200..=299).contains(&status) => {
            match status_body_advertises(capability, body.as_deref()) {
                Ok(true) => CapabilityAvailability {
                    capability,
                    available: true,
                    status_route: Some(route),
                    degradation: None,
                },
                Ok(false) => unavailable(
                    capability,
                    Some(route),
                    CapabilityDegradationReason::NotAdvertised,
                    "daemon status route does not advertise this capability",
                    Some(status),
                ),
                Err(message) => unavailable(
                    capability,
                    Some(route),
                    CapabilityDegradationReason::InvalidStatusBody,
                    message,
                    Some(status),
                ),
            }
        }
        ProbeObservation::Http {
            status: status @ (401 | 403),
            ..
        } => unavailable(
            capability,
            Some(route),
            CapabilityDegradationReason::Unauthorized,
            "daemon status route requires authorization for this process",
            Some(status),
        ),
        ProbeObservation::Http { status: 404, .. } => unavailable(
            capability,
            Some(route),
            CapabilityDegradationReason::MissingEndpoint,
            "daemon status route is not registered",
            Some(404),
        ),
        ProbeObservation::Http { status, .. } => unavailable(
            capability,
            Some(route),
            CapabilityDegradationReason::UnexpectedStatus,
            format!("daemon status route returned HTTP {status}"),
            Some(status),
        ),
        ProbeObservation::TransportError(message) => unavailable(
            capability,
            Some(route),
            CapabilityDegradationReason::Unreachable,
            format!("daemon status route failed: {message}"),
            None,
        ),
    }
}

fn status_body_advertises(capability: AiCapability, body: Option<&str>) -> Result<bool, String> {
    let body = body
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "daemon status route returned an empty body".to_string())?;
    let value: Value = serde_json::from_str(body)
        .map_err(|error| format!("daemon status body is not valid JSON: {error}"))?;

    let paths: &[&[&str]] = match capability {
        AiCapability::AudioTranscribe => &[
            &["transcription_enabled"],
            &["openai_compatible_audio", "transcription_enabled"],
            &["voice", "openai_compatible_audio", "transcription_enabled"],
        ],
        AiCapability::AudioTranslate => &[
            &["translation_enabled"],
            &["openai_compatible_audio", "translation_enabled"],
            &["voice", "openai_compatible_audio", "translation_enabled"],
        ],
        AiCapability::VisionExtract => &[
            &["vision_extract"],
            &["vision_extract_enabled"],
            &["extraction_enabled"],
            &["capabilities", "vision_extract"],
            &["enabled"],
        ],
        AiCapability::TextGenerate => &[
            &["text_generate"],
            &["text_generate_enabled"],
            &["generation_enabled"],
            &["capabilities", "text_generate"],
            &["enabled"],
        ],
        AiCapability::Embed => &[
            &["embed"],
            &["embedding_enabled"],
            &["embeddings_enabled"],
            &["capabilities", "embed"],
            &["enabled"],
        ],
    };

    paths
        .iter()
        .find_map(|path| bool_at_path(&value, path))
        .ok_or_else(|| {
            format!(
                "daemon status body does not advertise {}",
                capability.as_str()
            )
        })
}

fn bool_at_path(value: &Value, path: &[&str]) -> Option<bool> {
    path.iter()
        .try_fold(value, |current, key| current.get(*key))
        .and_then(Value::as_bool)
}

fn unavailable(
    capability: AiCapability,
    status_route: Option<CapabilityStatusRoute>,
    reason: CapabilityDegradationReason,
    message: impl Into<String>,
    http_status: Option<u16>,
) -> CapabilityAvailability {
    CapabilityAvailability {
        capability,
        available: false,
        status_route,
        degradation: Some(CapabilityDegradation {
            capability,
            reason,
            message: message.into(),
            http_status,
        }),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ProbeObservation {
    Http { status: u16, body: Option<String> },
    TransportError(String),
}

trait DaemonProbeTransport {
    fn status(&self, base_url: &str, method: &str, path: &str) -> ProbeObservation;
}

struct UreqProbeTransport;

impl DaemonProbeTransport for UreqProbeTransport {
    fn status(&self, base_url: &str, method: &str, path: &str) -> ProbeObservation {
        let url = format!("{}{}", base_url.trim_end_matches('/'), path);
        match ureq::request(method, &url).timeout(PROBE_TIMEOUT).call() {
            Ok(response) => ProbeObservation::Http {
                status: response.status(),
                body: response.into_string().ok(),
            },
            Err(ureq::Error::Status(status, response)) => ProbeObservation::Http {
                status,
                body: response.into_string().ok(),
            },
            Err(error) => ProbeObservation::TransportError(error.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    #[test]
    fn capability_status_routes() {
        assert_eq!(
            capability_status_route(AiCapability::VisionExtract).map(|route| route.path),
            Some("/api/llm/vision/status")
        );
        assert_eq!(
            capability_status_route(AiCapability::AudioTranscribe).map(|route| route.path),
            Some("/api/voice/status")
        );
        assert_eq!(
            capability_status_route(AiCapability::AudioTranslate).map(|route| route.path),
            Some("/api/voice/status")
        );
        assert_eq!(
            capability_status_route(AiCapability::TextGenerate).map(|route| route.path),
            Some("/api/llm/status")
        );

        let report = probe_daemon_capabilities_with("http://daemon.test", &FakeTransport::new([]));
        assert!(
            report
                .availability(AiCapability::VisionExtract)
                .is_some_and(|availability| !availability.available)
        );
        assert!(
            report
                .availability(AiCapability::AudioTranscribe)
                .is_some_and(|availability| !availability.available)
        );
        assert!(
            report
                .availability(AiCapability::AudioTranslate)
                .is_some_and(|availability| !availability.available)
        );
        assert!(
            report
                .availability(AiCapability::TextGenerate)
                .is_some_and(|availability| !availability.available)
        );
        assert!(report.availability(AiCapability::Embed).is_some());
        assert!(
            CapabilityProbeReport {
                base_url: "http://daemon.test".to_string(),
                capabilities: Vec::new(),
            }
            .availability(AiCapability::Embed)
            .is_none()
        );
    }

    #[test]
    fn embed_status_body_requires_advertised_capability() {
        assert_eq!(
            status_body_advertises(AiCapability::Embed, Some(r#"{"embedding_enabled":true}"#)),
            Ok(true)
        );
        assert!(status_body_advertises(AiCapability::Embed, Some(r#"{}"#)).is_err());
    }

    #[test]
    fn attachments_not_vision_extraction() {
        let transport = FakeTransport::new([(
            ("POST", "/api/chat/attachments"),
            ProbeObservation::Http {
                status: 200,
                body: Some(r#"{"enabled":true}"#.to_string()),
            },
        )]);

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert!(
            report
                .availability(AiCapability::VisionExtract)
                .is_some_and(|availability| !availability.available)
        );
        assert!(
            !transport
                .requests()
                .contains(&("POST".to_string(), "/api/chat/attachments".to_string()))
        );
        assert!(
            transport
                .requests()
                .contains(&("GET".to_string(), "/api/llm/vision/status".to_string()))
        );
    }

    #[test]
    fn status_body_capability_truth() {
        let transport = FakeTransport::new([(
            ("GET", "/api/voice/status"),
            ProbeObservation::Http {
                status: 200,
                body: Some(
                    r#"{"transcription_enabled":true,"translation_enabled":false}"#.to_string(),
                ),
            },
        )]);

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert!(
            report
                .availability(AiCapability::AudioTranscribe)
                .is_some_and(|availability| availability.available)
        );
        assert!(
            report
                .availability(AiCapability::AudioTranslate)
                .is_some_and(|availability| !availability.available)
        );
    }

    #[test]
    fn status_route_is_availability_truth() {
        let transport = FakeTransport::new([(
            ("GET", "/api/providers/models"),
            ProbeObservation::Http {
                status: 200,
                body: Some(r#"{"capabilities":{"text_generate":true}}"#.to_string()),
            },
        )]);

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert!(
            report
                .availability(AiCapability::TextGenerate)
                .is_some_and(|availability| !availability.available)
        );
        assert!(
            !transport
                .requests()
                .contains(&("GET".to_string(), "/api/providers/models".to_string()))
        );
        assert!(
            transport
                .requests()
                .contains(&("GET".to_string(), "/api/llm/status".to_string()))
        );
    }

    struct FakeTransport {
        responses: HashMap<(&'static str, &'static str), ProbeObservation>,
        requests: RefCell<Vec<(String, String)>>,
    }

    impl FakeTransport {
        fn new(
            responses: impl IntoIterator<Item = ((&'static str, &'static str), ProbeObservation)>,
        ) -> Self {
            Self {
                responses: responses.into_iter().collect(),
                requests: RefCell::new(Vec::new()),
            }
        }

        fn requests(&self) -> Vec<(String, String)> {
            self.requests.borrow().clone()
        }
    }

    impl DaemonProbeTransport for FakeTransport {
        fn status(&self, _base_url: &str, method: &str, path: &str) -> ProbeObservation {
            self.requests
                .borrow_mut()
                .push((method.to_string(), path.to_string()));
            self.responses
                .get(&(method, path))
                .cloned()
                .unwrap_or(ProbeObservation::Http {
                    status: 404,
                    body: None,
                })
        }
    }
}
