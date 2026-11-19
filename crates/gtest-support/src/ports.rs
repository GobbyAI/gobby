//! Free loopback ports for a full-daemon launch.
//!
//! The front door and the Python backend listen on a pair (`port` and `port + 100`), and the
//! WebSocket listener uses the same layout. Ports the shared stack owns and the isolated test hub
//! (60891, 60892) are never handed out.

use std::net::TcpListener;

use crate::hub_guard::SHARED_PORTS;

/// The live hub's and the test hub's ports, which no launch may use.
pub const HUB_PORTS: [u16; 2] = [60891, 60892];

/// The offset between a public port and its backend port (tests/e2e/conftest.py:1106-1117).
pub const BACKEND_OFFSET: u16 = 100;

/// The four ports one daemon uses: the front door and WebSocket listeners, and their backends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DaemonPorts {
    /// The HTTP front door.
    pub http: u16,
    /// The WebSocket listener.
    pub websocket: u16,
}

impl DaemonPorts {
    /// The HTTP backend the front door proxies to.
    pub fn http_backend(self) -> u16 {
        self.http + BACKEND_OFFSET
    }

    /// The WebSocket backend.
    pub fn websocket_backend(self) -> u16 {
        self.websocket + BACKEND_OFFSET
    }
}

/// Whether `port` is one a launch must never take.
pub fn is_excluded(port: u16) -> bool {
    SHARED_PORTS.contains(&port) || HUB_PORTS.contains(&port)
}

/// Whether `port` is free on `127.0.0.1` right now and not excluded.
pub fn is_free(port: u16) -> bool {
    !is_excluded(port) && TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// A kernel-chosen free loopback port that is not excluded.
pub fn ephemeral_port() -> std::io::Result<u16> {
    loop {
        let port = TcpListener::bind(("127.0.0.1", 0))?.local_addr()?.port();
        if !is_excluded(port) && port.checked_add(BACKEND_OFFSET).is_some() {
            return Ok(port);
        }
    }
}

/// Four distinct free ports in one layout: the public pair and each pair's backend at `+100`.
/// Returns `None` when no layout is free after `attempts` tries.
pub fn daemon_ports(attempts: usize) -> std::io::Result<Option<DaemonPorts>> {
    for _ in 0..attempts {
        let http = ephemeral_port()?;
        let websocket = ephemeral_port()?;
        if http == websocket {
            continue;
        }
        let candidates = [
            http,
            websocket,
            http + BACKEND_OFFSET,
            websocket + BACKEND_OFFSET,
        ];
        let distinct = candidates
            .iter()
            .enumerate()
            .all(|(index, port)| !candidates[..index].contains(port));
        if distinct && is_free(http + BACKEND_OFFSET) && is_free(websocket + BACKEND_OFFSET) {
            return Ok(Some(DaemonPorts { http, websocket }));
        }
    }
    Ok(None)
}
