use serde::Deserialize;

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Backend {
    pub name: String,
    pub url: String,
    pub probe: String,
    #[serde(default)]
    pub auth_token: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackendDefault {
    pub name: &'static str,
    pub url: &'static str,
    pub probe: &'static str,
    pub auth_token: &'static str,
}

impl BackendDefault {
    pub fn to_backend(self) -> Backend {
        Backend {
            name: self.name.to_string(),
            url: self.url.to_string(),
            probe: self.probe.to_string(),
            auth_token: self.auth_token.to_string(),
        }
    }
}

pub const DEFAULT_BACKENDS: &[BackendDefault] = &[
    BackendDefault {
        name: "lmstudio",
        url: "http://localhost:1234",
        probe: "/v1/models",
        auth_token: "lmstudio",
    },
    BackendDefault {
        name: "ollama",
        url: "http://localhost:11434",
        probe: "/api/tags",
        auth_token: "ollama",
    },
];

pub fn default_backends() -> Vec<Backend> {
    DEFAULT_BACKENDS
        .iter()
        .copied()
        .map(BackendDefault::to_backend)
        .collect()
}

pub fn backend_api_base(backend: &Backend) -> String {
    format!("{}/v1", backend.url.trim_end_matches('/'))
}

/// Probe backends in order, return the first that responds successfully.
#[cfg(feature = "local_backend")]
pub fn detect_backend(backends: &[Backend], timeout_ms: u64) -> Option<Backend> {
    for backend in backends {
        if validate_backend(backend, timeout_ms) {
            return Some(backend.clone());
        }
    }
    None
}

/// Validate that a specific backend is reachable.
#[cfg(feature = "local_backend")]
pub fn validate_backend(backend: &Backend, timeout_ms: u64) -> bool {
    let timeout = std::time::Duration::from_millis(timeout_ms);
    let url = format!("{}{}", backend.url, backend.probe);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(timeout)
        .timeout_read(timeout)
        .build();
    agent.get(&url).call().is_ok()
}

#[cfg(all(test, feature = "local_backend"))]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;

    fn reachable_backend() -> Backend {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            if let Ok((mut stream, _)) = listener.accept() {
                let mut buffer = [0_u8; 1024];
                let _ = stream.read(&mut buffer);
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                );
            }
        });

        Backend {
            name: "reachable".into(),
            url: format!("http://{}", addr),
            probe: "/v1/models".into(),
            auth_token: "token".into(),
        }
    }

    fn unreachable_backend() -> Backend {
        Backend {
            name: "unreachable".into(),
            url: "http://127.0.0.1:9".into(),
            probe: "/".into(),
            auth_token: String::new(),
        }
    }

    #[test]
    fn detects_first_reachable() {
        let reachable = reachable_backend();
        let backends = vec![unreachable_backend(), reachable.clone()];

        assert_eq!(detect_backend(&backends, 500), Some(reachable));
    }
}
