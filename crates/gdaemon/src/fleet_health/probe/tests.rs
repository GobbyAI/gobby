use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

use super::*;
use crate::fleet_health::Probe;

/// Serve one canned response to the first connection, then stop.
async fn serve_once(response: &'static str) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = [0u8; 1024];
        let _ = stream.read(&mut request).await;
        stream.write_all(response.as_bytes()).await.expect("write");
        let _ = stream.shutdown().await;
    });
    port
}

#[tokio::test]
async fn ok_response_is_reported_with_its_status() {
    let port = serve_once("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok").await;
    let probe = probe_health("127.0.0.1", port, Duration::from_secs(5)).await;
    assert!(
        matches!(probe, Probe::Responded { status: 200, .. }),
        "{probe:?}"
    );
}

#[tokio::test]
async fn error_status_is_reported_not_treated_as_failure() {
    let port = serve_once("HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n").await;
    let probe = probe_health("127.0.0.1", port, Duration::from_secs(5)).await;
    assert!(
        matches!(probe, Probe::Responded { status: 503, .. }),
        "{probe:?}"
    );
}

#[tokio::test]
async fn refused_connection_is_a_failed_probe() {
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        listener.local_addr().expect("addr").port()
    };
    let probe = probe_health("127.0.0.1", port, Duration::from_secs(5)).await;
    assert!(matches!(probe, Probe::Failed { .. }), "{probe:?}");
}

#[tokio::test]
async fn silent_endpoint_times_out_as_a_failed_probe() {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    tokio::spawn(async move {
        let (_stream, _) = listener.accept().await.expect("accept");
        tokio::time::sleep(Duration::from_secs(30)).await;
    });
    let started = std::time::Instant::now();
    let probe = probe_health("127.0.0.1", port, Duration::from_millis(200)).await;
    assert!(matches!(probe, Probe::Failed { .. }), "{probe:?}");
    assert!(started.elapsed() < Duration::from_secs(5));
}

#[tokio::test]
async fn refused_connection_on_the_named_host_is_a_failed_probe() {
    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        listener.local_addr().expect("addr").port()
    };
    let probe = probe_health("localhost", port, Duration::from_secs(5)).await;
    assert!(matches!(probe, Probe::Failed { .. }), "{probe:?}");
}

#[test]
fn wildcard_binds_are_dialled_on_loopback_and_concrete_hosts_are_kept() {
    assert_eq!(dial_host("0.0.0.0"), "127.0.0.1");
    assert_eq!(dial_host("::"), "127.0.0.1");
    assert_eq!(dial_host(""), "127.0.0.1");
    assert_eq!(dial_host("127.0.0.1"), "127.0.0.1");
    assert_eq!(dial_host("100.64.0.7"), "100.64.0.7");
}

#[test]
fn ipv6_hosts_are_bracketed_in_the_host_header() {
    assert_eq!(host_header("::1"), "[::1]");
    assert_eq!(host_header("127.0.0.1"), "127.0.0.1");
    assert_eq!(host_header("[::1]"), "[::1]");
}
