//! Gobby daemon capability probe for optional gwiki integrations.

use std::time::Duration;

use serde::Serialize;

const PROBE_TIMEOUT: Duration = Duration::from_millis(750);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DaemonCapability {
    Embeddings,
    Synthesis,
    Vision,
    Transcription,
    AgentDispatch,
    SessionEvents,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DegradationReason {
    MissingEndpoint,
    Unauthorized,
    Unreachable,
    UnexpectedStatus,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EndpointShape {
    pub method: &'static str,
    pub path: &'static str,
    pub request_shape: &'static str,
    pub response_shape: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CapabilityAvailability {
    pub capability: DaemonCapability,
    pub available: bool,
    pub optional: bool,
    pub endpoint: EndpointShape,
    pub degradation: Option<DaemonDegradation>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DaemonDegradation {
    pub capability: DaemonCapability,
    pub endpoint: &'static str,
    pub reason: DegradationReason,
    pub message: String,
    pub fallback: &'static str,
    pub http_status: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DaemonCapabilityReport {
    pub base_url: String,
    pub embeddings: CapabilityAvailability,
    pub synthesis: CapabilityAvailability,
    pub vision: CapabilityAvailability,
    pub transcription: CapabilityAvailability,
    pub agent_dispatch: CapabilityAvailability,
    pub session_events: CapabilityAvailability,
    pub degraded: Vec<DaemonDegradation>,
}

#[derive(Debug, Clone, Copy)]
struct EndpointContract {
    capability: DaemonCapability,
    optional: bool,
    method: &'static str,
    path: &'static str,
    probe_method: &'static str,
    request_shape: &'static str,
    response_shape: &'static str,
    fallback: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ProbeObservation {
    HttpStatus(u16),
    TransportError(String),
}

trait DaemonProbeTransport: Sync {
    fn status(&self, base_url: &str, method: &str, path: &str) -> ProbeObservation;
}

struct UreqProbeTransport;

impl DaemonProbeTransport for UreqProbeTransport {
    fn status(&self, base_url: &str, method: &str, path: &str) -> ProbeObservation {
        let url = format!("{}{}", base_url.trim_end_matches('/'), path);
        match ureq::request(method, &url).timeout(PROBE_TIMEOUT).call() {
            Ok(response) => ProbeObservation::HttpStatus(response.status()),
            Err(ureq::Error::Status(status, _)) => ProbeObservation::HttpStatus(status),
            Err(error) => ProbeObservation::TransportError(error.to_string()),
        }
    }
}

const EMBEDDINGS: EndpointContract = EndpointContract {
    capability: DaemonCapability::Embeddings,
    optional: true,
    method: "POST",
    path: "/api/memories/embeddings/reindex",
    probe_method: "OPTIONS",
    request_shape: "query: project_id?; body: none",
    response_shape: "object with embedding reindex counts or error detail",
    fallback: "Disable daemon-backed arbitrary embedding generation; keep lexical/wiki indexing paths available.",
};

const SYNTHESIS: EndpointContract = EndpointContract {
    capability: DaemonCapability::Synthesis,
    optional: true,
    method: "GET",
    path: "/api/providers/models",
    probe_method: "GET",
    request_shape: "none",
    response_shape: r#"{"providers":[{"provider","available","models","source","startup_error",...}]}"#,
    fallback: "Skip daemon-assisted synthesis and return source/manually-authored wiki content.",
};

const VISION: EndpointContract = EndpointContract {
    capability: DaemonCapability::Vision,
    optional: true,
    method: "GET",
    path: "/api/llm/vision/status",
    probe_method: "GET",
    request_shape: "none",
    response_shape: "vision status object advertising vision_extract support",
    fallback: "Keep raw image assets and surface filename/metadata only; skip visual extraction.",
};

const TRANSCRIPTION: EndpointContract = EndpointContract {
    capability: DaemonCapability::Transcription,
    optional: true,
    method: "GET",
    path: "/api/voice/status",
    probe_method: "GET",
    request_shape: "none",
    response_shape: "voice status object advertising transcription_enabled/translation_enabled",
    fallback: "Keep raw audio assets and require supplied transcripts; skip daemon transcription.",
};

const AGENT_DISPATCH: EndpointContract = EndpointContract {
    capability: DaemonCapability::AgentDispatch,
    optional: true,
    method: "POST",
    path: "/api/agents/spawn",
    probe_method: "OPTIONS",
    request_shape: "JSON AgentSpawnRequest: task_id, agent_name?, prompt?, provider?, model?, isolation?, workflow?",
    response_shape: "AgentSpawnResponse with success plus run_id/child_session_id/conversation_id or error",
    fallback: "Return dispatch degradation metadata; do not spawn local subprocesses from gwiki.",
};

const SESSION_EVENTS: EndpointContract = EndpointContract {
    capability: DaemonCapability::SessionEvents,
    optional: true,
    method: "GET",
    path: "/api/sessions",
    probe_method: "GET",
    request_shape: "query filters such as source?, project_id?, status?, limit?, offset?",
    response_shape: "session listing object with sessions and pagination/count fields",
    fallback: "Disable live session monitoring and rely on explicit command output.",
};

pub fn probe_daemon_capabilities() -> DaemonCapabilityReport {
    let base_url = gobby_core::daemon_url::daemon_url();
    probe_daemon_capabilities_at(&base_url)
}

pub fn probe_daemon_capabilities_at(base_url: &str) -> DaemonCapabilityReport {
    probe_daemon_capabilities_with(base_url, &UreqProbeTransport)
}

fn probe_daemon_capabilities_with(
    base_url: &str,
    transport: &impl DaemonProbeTransport,
) -> DaemonCapabilityReport {
    let contracts = [
        EMBEDDINGS,
        SYNTHESIS,
        VISION,
        TRANSCRIPTION,
        AGENT_DISPATCH,
        SESSION_EVENTS,
    ];
    let first_observation =
        transport.status(base_url, contracts[0].probe_method, contracts[0].path);
    let availabilities = if let ProbeObservation::TransportError(message) = first_observation {
        contracts
            .into_iter()
            .map(|contract| unreachable_availability(contract, &message))
            .collect::<Vec<_>>()
    } else {
        let mut availabilities = vec![availability_for_observation(
            contracts[0],
            first_observation,
        )];
        let rest = std::thread::scope(|scope| {
            contracts[1..]
                .iter()
                .copied()
                .map(|contract| {
                    (
                        contract,
                        scope.spawn(move || probe_contract(base_url, transport, contract)),
                    )
                })
                .map(|(contract, handle)| match handle.join() {
                    Ok(availability) => availability,
                    Err(_) => unreachable_availability(contract, "capability probe panicked"),
                })
                .collect::<Vec<_>>()
        });
        availabilities.extend(rest);
        availabilities
    };
    let mut availabilities = availabilities.into_iter();
    let embeddings = availabilities.next().expect("embeddings probe");
    let synthesis = availabilities.next().expect("synthesis probe");
    let vision = availabilities.next().expect("vision probe");
    let transcription = availabilities.next().expect("transcription probe");
    let agent_dispatch = availabilities.next().expect("agent dispatch probe");
    let session_events = availabilities.next().expect("session events probe");

    let degraded = [
        &embeddings,
        &synthesis,
        &vision,
        &transcription,
        &agent_dispatch,
        &session_events,
    ]
    .into_iter()
    .filter_map(|availability| availability.degradation.clone())
    .collect();

    DaemonCapabilityReport {
        base_url: base_url.to_string(),
        embeddings,
        synthesis,
        vision,
        transcription,
        agent_dispatch,
        session_events,
        degraded,
    }
}

fn probe_contract(
    base_url: &str,
    transport: &impl DaemonProbeTransport,
    contract: EndpointContract,
) -> CapabilityAvailability {
    let observation = transport.status(base_url, contract.probe_method, contract.path);
    availability_for_observation(contract, observation)
}

fn availability_for_observation(
    contract: EndpointContract,
    observation: ProbeObservation,
) -> CapabilityAvailability {
    let degradation = degradation_for_observation(contract, observation);
    CapabilityAvailability {
        capability: contract.capability,
        available: degradation.is_none(),
        optional: contract.optional,
        endpoint: EndpointShape {
            method: contract.method,
            path: contract.path,
            request_shape: contract.request_shape,
            response_shape: contract.response_shape,
        },
        degradation,
    }
}

fn unreachable_availability(contract: EndpointContract, message: &str) -> CapabilityAvailability {
    CapabilityAvailability {
        capability: contract.capability,
        available: false,
        optional: contract.optional,
        endpoint: EndpointShape {
            method: contract.method,
            path: contract.path,
            request_shape: contract.request_shape,
            response_shape: contract.response_shape,
        },
        degradation: Some(degradation(
            contract,
            DegradationReason::Unreachable,
            format!("daemon transport failed: {message}"),
            None,
        )),
    }
}

fn degradation_for_observation(
    contract: EndpointContract,
    observation: ProbeObservation,
) -> Option<DaemonDegradation> {
    match observation {
        ProbeObservation::HttpStatus(status) if (200..=299).contains(&status) => None,
        ProbeObservation::HttpStatus(405) if contract.probe_method == "OPTIONS" => None,
        ProbeObservation::HttpStatus(status @ (401 | 403)) => Some(degradation(
            contract,
            DegradationReason::Unauthorized,
            "daemon endpoint requires authorization for this process",
            Some(status),
        )),
        ProbeObservation::HttpStatus(404) => Some(degradation(
            contract,
            DegradationReason::MissingEndpoint,
            "daemon endpoint is not registered",
            Some(404),
        )),
        ProbeObservation::HttpStatus(status) => Some(degradation(
            contract,
            DegradationReason::UnexpectedStatus,
            format!("daemon endpoint returned HTTP {status}"),
            Some(status),
        )),
        ProbeObservation::TransportError(message) => Some(degradation(
            contract,
            DegradationReason::Unreachable,
            format!("daemon transport failed: {message}"),
            None,
        )),
    }
}

fn degradation(
    contract: EndpointContract,
    reason: DegradationReason,
    message: impl Into<String>,
    http_status: Option<u16>,
) -> DaemonDegradation {
    DaemonDegradation {
        capability: contract.capability,
        endpoint: contract.path,
        reason,
        message: message.into(),
        fallback: contract.fallback,
        http_status,
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;

    struct FakeTransport {
        statuses: HashMap<(&'static str, &'static str), ProbeObservation>,
    }

    impl FakeTransport {
        fn new(
            statuses: impl IntoIterator<Item = ((&'static str, &'static str), ProbeObservation)>,
        ) -> Self {
            Self {
                statuses: statuses.into_iter().collect(),
            }
        }
    }

    impl DaemonProbeTransport for FakeTransport {
        fn status(&self, _base_url: &str, method: &str, path: &str) -> ProbeObservation {
            self.statuses
                .get(&(method, path))
                .cloned()
                .unwrap_or(ProbeObservation::HttpStatus(200))
        }
    }

    #[test]
    fn missing_optional_endpoint_degrades() {
        let transport = FakeTransport::new([
            (
                ("GET", "/api/llm/vision/status"),
                ProbeObservation::HttpStatus(404),
            ),
            (
                ("OPTIONS", "/api/agents/spawn"),
                ProbeObservation::HttpStatus(404),
            ),
        ]);

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert!(!report.vision.available);
        assert_eq!(
            report.vision.degradation.as_ref().map(|d| d.reason),
            Some(DegradationReason::MissingEndpoint)
        );
        assert!(
            report
                .degraded
                .iter()
                .any(|d| d.capability == DaemonCapability::Vision
                    && d.http_status == Some(404)
                    && d.fallback.contains("Keep raw image assets"))
        );
        assert!(!report.agent_dispatch.available);
        assert_eq!(
            report.agent_dispatch.degradation.as_ref().map(|d| d.reason),
            Some(DegradationReason::MissingEndpoint)
        );
        assert_eq!(
            report
                .agent_dispatch
                .degradation
                .as_ref()
                .and_then(|d| d.http_status),
            Some(404)
        );
    }

    #[test]
    fn safe_write_probe_method_not_allowed_still_means_endpoint_exists() {
        let transport = FakeTransport::new([(
            ("OPTIONS", "/api/memories/embeddings/reindex"),
            ProbeObservation::HttpStatus(405),
        )]);

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert!(report.embeddings.available);
        assert!(report.embeddings.degradation.is_none());
    }

    #[test]
    fn first_transport_failure_degrades_all_capabilities_without_more_probes() {
        struct FailingTransport {
            calls: AtomicUsize,
        }

        impl DaemonProbeTransport for FailingTransport {
            fn status(&self, _base_url: &str, _method: &str, _path: &str) -> ProbeObservation {
                self.calls.fetch_add(1, Ordering::SeqCst);
                ProbeObservation::TransportError("connection refused".to_string())
            }
        }

        let transport = FailingTransport {
            calls: AtomicUsize::new(0),
        };

        let report = probe_daemon_capabilities_with("http://daemon.test", &transport);

        assert_eq!(transport.calls.load(Ordering::SeqCst), 1);
        assert_eq!(report.degraded.len(), 6);
        assert!(report.degraded.iter().all(|degradation| {
            degradation.reason == DegradationReason::Unreachable
                && degradation.message.contains("connection refused")
        }));
    }

    #[test]
    fn probe_thread_panic_degrades_that_capability() {
        struct PanickingTransport;

        impl DaemonProbeTransport for PanickingTransport {
            fn status(&self, _base_url: &str, _method: &str, path: &str) -> ProbeObservation {
                if path == VISION.path {
                    panic!("vision probe panic");
                }
                ProbeObservation::HttpStatus(200)
            }
        }

        let report = probe_daemon_capabilities_with("http://daemon.test", &PanickingTransport);

        assert!(report.embeddings.available);
        assert!(!report.vision.available);
        let degradation = report.vision.degradation.expect("vision degraded");
        assert_eq!(degradation.reason, DegradationReason::Unreachable);
        assert!(degradation.message.contains("capability probe panicked"));
    }
}
