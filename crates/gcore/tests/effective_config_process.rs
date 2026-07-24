#![cfg(feature = "ai")]

use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

use gobby_core::ai::effective_config::daemon_mode_layers;
use gobby_core::config::ConfigSource;
use gobby_core::runtime_mode::RUNTIME_MODE_ENV;

#[test]
fn daemon_effective_config_is_fetched_once_per_process() {
    let home = tempfile::tempdir().expect("temp Gobby home");
    std::fs::write(home.path().join("local_cli_token"), "effective-token\n")
        .expect("write local token");
    let (daemon_url, request) = spawn_effective_config_server();

    unsafe {
        std::env::set_var("GOBBY_HOME", home.path());
        std::env::set_var(RUNTIME_MODE_ENV, "auto");
        std::env::set_var("GOBBY_DAEMON_URL", &daemon_url);
    }

    let (mut first, _) = daemon_mode_layers()
        .expect("first daemon config read")
        .expect("daemon mode");
    assert_eq!(
        first.config_value("ai.embeddings.model").as_deref(),
        Some("served-model")
    );

    unsafe {
        std::env::set_var(RUNTIME_MODE_ENV, "standalone");
        std::env::set_var("GOBBY_DAEMON_URL", "http://127.0.0.1:1");
    }
    let (mut second, _) = daemon_mode_layers()
        .expect("cached daemon config read")
        .expect("cached daemon mode");
    assert_eq!(
        second.config_value("ai.embeddings.model").as_deref(),
        Some("served-model")
    );

    let request = request.join().expect("daemon thread");
    assert!(request.starts_with("GET /api/config/effective HTTP/1.1"));
    assert!(
        request
            .lines()
            .any(|line| line.eq_ignore_ascii_case("Authorization: Bearer effective-token"))
    );
}

fn spawn_effective_config_server() -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind daemon");
    let address = listener.local_addr().expect("daemon address");
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        loop {
            let read = stream.read(&mut buffer).expect("read request");
            request.extend_from_slice(&buffer[..read]);
            if read == 0 || request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        let body = r#"{"config":{"ai.embeddings.model":"served-model"}}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        String::from_utf8(request).expect("request is UTF-8")
    });
    (format!("http://{address}"), handle)
}
