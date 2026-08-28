//! 3.5 gclient starts independently of the Python CLI.

use gobby_client::startup::{parse_args, start_session, HttpHealthClient, ProbeEnv, StartupError};
use gobby_client::teardown::{ModeBackend, TerminalGuard};
use std::io::{self, Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

struct CountingBackend {
    enters: Arc<AtomicUsize>,
}

impl CountingBackend {
    fn new() -> (Self, Arc<AtomicUsize>) {
        let enters = Arc::new(AtomicUsize::new(0));
        (
            Self {
                enters: Arc::clone(&enters),
            },
            enters,
        )
    }
}

impl ModeBackend for CountingBackend {
    fn enter(&mut self) -> io::Result<()> {
        self.enters.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }

    fn restore(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn closed_port_url() -> String {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
    let addr = listener.local_addr().expect("addr");
    drop(listener);
    format!("http://{addr}")
}

fn serve_health_json(body: &str) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind health");
    listener.set_nonblocking(false).expect("blocking accept");
    let addr = listener.local_addr().expect("addr");
    let body = body.to_string();
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept health");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("read timeout");
        let mut buf = [0u8; 4096];
        let mut collected = Vec::new();
        loop {
            match stream.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    collected.extend_from_slice(&buf[..n]);
                    if collected.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                Err(err) if err.kind() == io::ErrorKind::WouldBlock => break,
                Err(err) if err.kind() == io::ErrorKind::TimedOut => break,
                Err(_) => break,
            }
        }
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.flush();
    });
    (format!("http://{addr}"), handle)
}

fn env_at(url: &str) -> ProbeEnv {
    ProbeEnv {
        daemon_url: url.to_string(),
        token: None,
    }
}

#[test]
fn test_unreachable_daemon_before_raw_mode() {
    let url = closed_port_url();
    let args = parse_args(["gclient"]).expect("parse");
    let (backend, enters) = CountingBackend::new();
    let err = match start_session(args, env_at(&url), &HttpHealthClient::new(), backend) {
        Err(err) => err,
        Ok(_) => panic!("unreachable daemon succeeded"),
    };
    assert_eq!(
        enters.load(Ordering::SeqCst),
        0,
        "raw-mode must not run before the daemon probe fails"
    );
    let message = err.to_string();
    assert!(
        message.contains("gobby start"),
        "actionable recovery missing `gobby start`: {message}"
    );
    assert!(
        message.contains("gobby status"),
        "actionable recovery missing `gobby status`: {message}"
    );
    assert!(
        matches!(err, StartupError::Unreachable { .. }),
        "distinct unreachable error, got {err:?}"
    );
}

#[test]
fn test_reports_degraded_host_state() {
    let body = r#"{
        "status": "degraded",
        "degraded_services": ["gterm_host"],
        "gterm_host": {
            "enabled": true,
            "running": false,
            "adopted": true,
            "host_epoch": "epoch-7",
            "restart_count": 3,
            "last_error": "socket missing"
        }
    }"#;
    let (url, server) = serve_health_json(body);
    let args = parse_args(["gclient"]).expect("parse");
    let (backend, enters) = CountingBackend::new();
    let err = match start_session(args, env_at(&url), &HttpHealthClient::new(), backend) {
        Err(err) => err,
        Ok(_) => panic!("degraded host succeeded"),
    };
    let _ = server.join();
    assert_eq!(
        enters.load(Ordering::SeqCst),
        0,
        "must refuse before entering a dead backend"
    );
    let message = err.to_string();
    assert!(
        message.contains("adopted=true") || message.contains("adopted: true"),
        "quote adopted: {message}"
    );
    assert!(
        message.contains("running=false") || message.contains("running: false"),
        "quote running: {message}"
    );
    assert!(message.contains("epoch-7"), "quote epoch: {message}");
    assert!(message.contains("3"), "quote restart count: {message}");
    assert!(
        message.contains("socket missing"),
        "quote last error: {message}"
    );
    assert!(
        matches!(err, StartupError::DegradedHost { .. }),
        "distinct host-down error, got {err:?}"
    );
}

#[test]
fn project_flag_selects_workspace() {
    let body = r#"{
        "status": "ok",
        "degraded_services": [],
        "gterm_host": {
            "enabled": true,
            "running": true,
            "adopted": false,
            "host_epoch": "epoch-ok",
            "restart_count": 0,
            "last_error": null
        }
    }"#;
    let (url, server) = serve_health_json(body);
    let args = parse_args(["gclient", "--project", "proj-1"]).expect("parse --project");
    assert_eq!(args.project.as_deref(), Some("proj-1"));
    let (backend, enters) = CountingBackend::new();
    let (ready, guard): (_, TerminalGuard<CountingBackend>) =
        match start_session(args, env_at(&url), &HttpHealthClient::new(), backend) {
            Ok(ok) => ok,
            Err(err) => panic!("healthy daemon failed: {err}"),
        };
    let _ = server.join();
    assert_eq!(ready.project.as_deref(), Some("proj-1"));
    assert_eq!(enters.load(Ordering::SeqCst), 1);
    drop(guard);
}
