use std::ffi::OsString;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::thread;
use std::time::Duration;

use gobby_core::ai::effective_config::ai_source_from_daemon;
use gobby_core::config::{
    AiCapability, resolve_capability_binding, resolve_embedding_config_from_binding,
};

struct EnvGuard {
    gobby_home: Option<OsString>,
    daemon_url: Option<OsString>,
    gobby_port: Option<OsString>,
    managed_execution_bootstrap: Option<OsString>,
    agent_api_token: Option<OsString>,
}

impl EnvGuard {
    fn set(home: &Path, daemon_url: &str) -> Self {
        let guard = Self {
            gobby_home: std::env::var_os("GOBBY_HOME"),
            daemon_url: std::env::var_os("GOBBY_DAEMON_URL"),
            gobby_port: std::env::var_os("GOBBY_PORT"),
            managed_execution_bootstrap: std::env::var_os("GOBBY_MANAGED_EXECUTION_BOOTSTRAP"),
            agent_api_token: std::env::var_os("GOBBY_AGENT_API_TOKEN"),
        };
        std::fs::write(home.join("local_cli_token"), "effective-token\n")
            .expect("write local token");
        // SAFETY: this integration-test binary contains one test, so no peer
        // thread can observe these temporary process environment overrides.
        unsafe {
            std::env::set_var("GOBBY_HOME", home);
            std::env::set_var("GOBBY_DAEMON_URL", daemon_url);
            std::env::remove_var("GOBBY_PORT");
            std::env::remove_var("GOBBY_MANAGED_EXECUTION_BOOTSTRAP");
            std::env::remove_var("GOBBY_AGENT_API_TOKEN");
        }
        guard
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        // SAFETY: this guard restores the single test's process environment.
        unsafe {
            restore_var("GOBBY_HOME", &self.gobby_home);
            restore_var("GOBBY_DAEMON_URL", &self.daemon_url);
            restore_var("GOBBY_PORT", &self.gobby_port);
            restore_var(
                "GOBBY_MANAGED_EXECUTION_BOOTSTRAP",
                &self.managed_execution_bootstrap,
            );
            restore_var("GOBBY_AGENT_API_TOKEN", &self.agent_api_token);
        }
    }
}

unsafe fn restore_var(name: &str, value: &Option<OsString>) {
    match value {
        Some(value) => unsafe { std::env::set_var(name, value) },
        None => unsafe { std::env::remove_var(name) },
    }
}

fn spawn_effective_config_server() -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind daemon");
    let address = listener.local_addr().expect("daemon address");
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .expect("set request read timeout");
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        loop {
            let read = stream.read(&mut buffer).expect("read request");
            request.extend_from_slice(&buffer[..read]);
            if read == 0 || request.windows(4).any(|window| window == b"\r\n\r\n") {
                break;
            }
        }
        let body = r#"{"revision":7,"config":{"ai.embeddings.provider":"openai","ai.embeddings.api_base":"https://daemon.example/v1","ai.embeddings.model":"served-embed","ai.embeddings.query_prefix":"search:"}}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
        String::from_utf8(request).expect("request is utf-8")
    });
    (format!("http://{address}"), handle)
}

#[test]
fn reachable_daemon_resolves_ai_without_opening_primary_database() {
    let home = tempfile::tempdir().expect("temp gobby home");
    let (daemon_url, request) = spawn_effective_config_server();
    let _env = EnvGuard::set(home.path(), &daemon_url);
    let mut source = ai_source_from_daemon::<gobby_core::ai_context::NoPrimaryAiConfigSource>()
        .expect("daemon source");

    let binding = resolve_capability_binding(&mut source, AiCapability::Embed);
    let embedding =
        resolve_embedding_config_from_binding(&mut source, &binding).expect("embedding config");
    assert_eq!(embedding.model, "served-embed");
    assert_eq!(embedding.query_prefix.as_deref(), Some("search:"));

    let request = request.join().expect("daemon thread");
    assert!(request.starts_with("GET /api/config/effective HTTP/1.1"));
    assert!(
        request
            .lines()
            .any(|line| line.eq_ignore_ascii_case("Authorization: Bearer effective-token"))
    );
}
