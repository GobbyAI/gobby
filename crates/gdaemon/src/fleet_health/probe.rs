//! End-to-end probe of the daemon's public health endpoint, dialled at the address the daemon
//! binds (loopback when it binds a wildcard).

use std::io;
use std::net::IpAddr;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use super::classify::Probe;

/// Reads at most this much of the response; the health answer is far smaller.
const RESPONSE_LIMIT: u64 = 64 * 1024;
const LOOPBACK: &str = "127.0.0.1";

/// The address the probe dials for a daemon bound to `bind_host`. A wildcard or empty bind
/// listens on every interface, so it is reached on loopback; a concrete address is dialled
/// as bound, because the daemon is not reachable on loopback when it binds elsewhere.
pub fn dial_host(bind_host: &str) -> String {
    let host = unbracketed(bind_host);
    match host.parse::<IpAddr>() {
        Ok(ip) if ip.is_unspecified() => LOOPBACK.into(),
        _ if host.is_empty() => LOOPBACK.into(),
        _ => host.into(),
    }
}

/// The `Host` header value for `host`, with IPv6 literals bracketed.
pub fn host_header(host: &str) -> String {
    let host = unbracketed(host);
    if host.contains(':') {
        format!("[{host}]")
    } else {
        host.to_string()
    }
}

fn unbracketed(host: &str) -> &str {
    host.trim().trim_start_matches('[').trim_end_matches(']')
}

/// `GET /api/health` on `<host>:<port>`, bounded by `timeout`.
pub async fn probe_health(host: &str, port: u16, timeout: Duration) -> Probe {
    let started = Instant::now();
    let outcome = tokio::time::timeout(timeout, request_status(host, port)).await;
    let elapsed_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
    match outcome {
        Ok(Ok(status)) => Probe::Responded { status, elapsed_ms },
        Ok(Err(error)) => Probe::Failed {
            elapsed_ms,
            reason: error.to_string(),
        },
        Err(_) => Probe::Failed {
            elapsed_ms,
            reason: "timeout".into(),
        },
    }
}

async fn request_status(host: &str, port: u16) -> io::Result<u16> {
    let mut stream = TcpStream::connect((unbracketed(host), port)).await?;
    let request = format!(
        "GET /api/health HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",
        host_header(host)
    );
    stream.write_all(request.as_bytes()).await?;
    let mut response = Vec::new();
    (&mut stream)
        .take(RESPONSE_LIMIT)
        .read_to_end(&mut response)
        .await?;

    let status_line = response
        .split(|byte| *byte == b'\n')
        .next()
        .unwrap_or_default();
    let status_line = String::from_utf8_lossy(status_line);
    status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "malformed status line"))
}

#[cfg(test)]
#[path = "probe/tests.rs"]
mod tests;
