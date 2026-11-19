//! Generic loopback listener helpers shared by the gdaemon test targets. This crate has no
//! `gobby-*` dependency, so only the bind and address parts live here.
//! Helpers that serve a front door stay with the gdaemon tests.

use std::net::{Ipv4Addr, SocketAddr};

use tokio::net::TcpListener;

/// Bind a TCP listener on `127.0.0.1:port`. Port 0 asks the kernel for an ephemeral port.
pub async fn bind_loopback(port: u16) -> std::io::Result<TcpListener> {
    bind_addr(SocketAddr::new(Ipv4Addr::LOCALHOST.into(), port)).await
}

/// Bind a TCP listener on `addr`.
pub async fn bind_addr(addr: SocketAddr) -> std::io::Result<TcpListener> {
    TcpListener::bind(addr).await
}

/// The address a bound listener actually holds, which matters when it was bound to port 0.
pub fn local_addr(listener: &TcpListener) -> std::io::Result<SocketAddr> {
    listener.local_addr()
}
