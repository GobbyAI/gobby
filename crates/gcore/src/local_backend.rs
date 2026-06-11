#[cfg(feature = "local-backend")]
use std::io::{self, Read, Write};
#[cfg(feature = "local-backend")]
use std::net::{TcpStream, ToSocketAddrs};
#[cfg(feature = "local-backend")]
use std::time::Duration;

use serde::Deserialize;

#[cfg(feature = "local-backend")]
const MAX_PROBE_RESPONSE_BYTES: usize = 1024;

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct Backend {
    pub name: String,
    pub url: String,
    pub probe: String,
    #[serde(default)]
    pub auth_token: String,
}

/// Probe backends in order, return the first that responds successfully.
#[cfg(feature = "local-backend")]
pub fn detect_backend(backends: &[Backend], timeout_ms: u64) -> Option<Backend> {
    for backend in backends {
        if validate_backend(backend, timeout_ms) {
            return Some(backend.clone());
        }
    }
    None
}

/// Validate that a specific backend is reachable.
#[cfg(feature = "local-backend")]
pub fn validate_backend(backend: &Backend, timeout_ms: u64) -> bool {
    let timeout = Duration::from_millis(timeout_ms);
    let url = backend_probe_url(backend);
    let Some(target) = HttpProbeTarget::parse(&url) else {
        log::trace!(
            "local backend probe `{}` at {} failed: unsupported HTTP URL",
            backend.name,
            url
        );
        return false;
    };

    match send_probe_request(&target, backend.auth_token.trim(), timeout) {
        Ok(status) if (200..300).contains(&status) => true,
        Ok(status) => {
            log::trace!(
                "local backend probe `{}` at {} returned HTTP {}",
                backend.name,
                url,
                status
            );
            false
        }
        Err(error) => {
            log::trace!(
                "local backend probe `{}` at {} failed: {}",
                backend.name,
                url,
                error
            );
            false
        }
    }
}

#[cfg(feature = "local-backend")]
#[derive(Debug, Clone, PartialEq, Eq)]
struct HttpProbeTarget {
    host: String,
    port: u16,
    path: String,
}

#[cfg(feature = "local-backend")]
impl HttpProbeTarget {
    fn parse(url: &str) -> Option<Self> {
        let rest = url.strip_prefix("http://")?;
        let (authority, path) = rest.split_once('/').unwrap_or((rest, ""));
        let (host, port) = parse_http_authority(authority)?;
        Some(Self {
            host,
            port,
            path: format!("/{path}"),
        })
    }

    fn socket_addr(&self) -> String {
        if self.host.contains(':') {
            format!("[{}]:{}", self.host, self.port)
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }

    fn host_header(&self) -> String {
        if self.port == 80 {
            self.host.clone()
        } else if self.host.contains(':') {
            format!("[{}]:{}", self.host, self.port)
        } else {
            format!("{}:{}", self.host, self.port)
        }
    }
}

#[cfg(feature = "local-backend")]
fn parse_http_authority(authority: &str) -> Option<(String, u16)> {
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    if let Some(rest) = authority.strip_prefix('[') {
        let (host, suffix) = rest.split_once(']')?;
        if host.is_empty() {
            return None;
        }
        let port = if suffix.is_empty() {
            80
        } else {
            suffix.strip_prefix(':')?.parse().ok()?
        };
        return Some((host.to_string(), port));
    }
    if authority.contains('[') || authority.contains(']') {
        return None;
    }
    match authority.rsplit_once(':') {
        Some((host, port)) if !host.contains(':') && !host.is_empty() => {
            Some((host.to_string(), port.parse().ok()?))
        }
        Some(_) => None,
        None => Some((authority.to_string(), 80)),
    }
}

#[cfg(feature = "local-backend")]
fn send_probe_request(
    target: &HttpProbeTarget,
    auth_token: &str,
    timeout: Duration,
) -> io::Result<u16> {
    let addr = target
        .socket_addr()
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no resolved address"))?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;

    let mut request = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nUser-Agent: gobby-core\r\nConnection: close\r\n",
        target.path,
        target.host_header()
    );
    if !auth_token.is_empty() {
        request.push_str("Authorization: Bearer ");
        request.push_str(auth_token);
        request.push_str("\r\n");
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes())?;

    let mut response = Vec::new();
    let mut chunk = [0_u8; 128];
    while response.len() < MAX_PROBE_RESPONSE_BYTES {
        let read = match stream.read(&mut chunk) {
            Ok(read) => read,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                ) && !response.is_empty() =>
            {
                break;
            }
            Err(error) => return Err(error),
        };
        if read == 0 {
            break;
        }
        response.extend_from_slice(&chunk[..read]);
        if response.contains(&b'\n') {
            break;
        }
    }
    parse_http_status(&response)
}

#[cfg(feature = "local-backend")]
fn parse_http_status(response: &[u8]) -> io::Result<u16> {
    let response = String::from_utf8_lossy(response);
    response
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|status| status.parse().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "missing HTTP status"))
}

#[cfg(feature = "local-backend")]
fn backend_probe_url(backend: &Backend) -> String {
    let base = backend.url.trim_end_matches('/');
    let probe = backend.probe.trim_start_matches('/');
    if probe.is_empty() {
        base.to_string()
    } else {
        format!("{base}/{probe}")
    }
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "local-backend")]
    mod http {
        use super::super::{Backend, backend_probe_url, detect_backend};
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::thread;

        fn reachable_backend() -> (Backend, thread::JoinHandle<String>) {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = listener.local_addr().unwrap();
            let handle = thread::spawn(move || {
                if let Ok((mut stream, _)) = listener.accept() {
                    let mut buffer = [0_u8; 1024];
                    let read = stream.read(&mut buffer).unwrap_or(0);
                    let _ = stream.write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                    );
                    return String::from_utf8_lossy(&buffer[..read]).to_string();
                }
                String::new()
            });

            (
                Backend {
                    name: "reachable".into(),
                    url: format!("http://{}", addr),
                    probe: "/v1/models".into(),
                    auth_token: "token".into(),
                },
                handle,
            )
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
            let (reachable, handle) = reachable_backend();
            let backends = vec![unreachable_backend(), reachable.clone()];

            assert_eq!(detect_backend(&backends, 500), Some(reachable));
            let request = handle.join().expect("probe request thread");
            assert!(has_header(&request, "authorization", "Bearer token"));
        }

        #[test]
        fn probe_url_uses_exactly_one_separator() {
            let backend = Backend {
                name: "test".into(),
                url: "http://localhost:1234/".into(),
                probe: "/v1/models".into(),
                auth_token: String::new(),
            };

            assert_eq!(
                backend_probe_url(&backend),
                "http://localhost:1234/v1/models"
            );

            let backend = Backend {
                probe: String::new(),
                ..backend
            };
            assert_eq!(backend_probe_url(&backend), "http://localhost:1234");
        }

        fn has_header(request: &str, name: &str, value: &str) -> bool {
            request.lines().any(|line| {
                line.split_once(':').is_some_and(|(header, actual)| {
                    header.trim().eq_ignore_ascii_case(name) && actual.trim() == value
                })
            })
        }
    }
}
