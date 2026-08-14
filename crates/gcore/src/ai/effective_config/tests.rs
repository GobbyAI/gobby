use std::cell::Cell;
use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

use tempfile::TempDir;

use super::*;
use crate::ai_context::{AiConfigSource, NoPrimaryAiConfigSource};
use crate::config::{
    AiCapability, ConfigSource, DaemonOrPrimary, DaemonServedConfig, is_machine_config_key,
    resolve_capability_binding, resolve_embedding_config_from_binding,
};
use crate::local_token::{AUTHORIZATION_HEADER, LOCAL_CLI_TOKEN_FILENAME};
use crate::test_http::{
    RequestHandle, read_http_request, spawn_json_response, spawn_json_response_with_status,
};

fn temp_home() -> TempDir {
    tempfile::tempdir().expect("temp gobby home")
}

/// Env override guard on the crate-wide `TEST_ENV_LOCK`, keeping this test
/// process on exactly one env lock so overrides can never interleave with
/// the other guarded env-mutating tests in this binary.
struct EnvVarsGuard {
    _lock: std::sync::MutexGuard<'static, ()>,
    saved: Vec<(&'static str, Option<std::ffi::OsString>)>,
}

impl EnvVarsGuard {
    fn set(vars: &[(&'static str, Option<&str>)]) -> Self {
        let lock = crate::config::TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let saved = vars
            .iter()
            .map(|(name, _)| (*name, std::env::var_os(name)))
            .collect();
        // SAFETY: TEST_ENV_LOCK serializes every env mutation in this test
        // process, and the guard restores the saved values before releasing
        // the lock.
        unsafe {
            for (name, value) in vars {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
        Self { _lock: lock, saved }
    }
}

impl Drop for EnvVarsGuard {
    fn drop(&mut self) {
        // SAFETY: the guard still holds TEST_ENV_LOCK while restoring the
        // original values.
        unsafe {
            for (name, value) in &self.saved {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

fn has_header(request: &str, name: &str, value: &str) -> bool {
    request.lines().any(|line| {
        let Some((header_name, header_value)) = line.split_once(':') else {
            return false;
        };
        header_name.eq_ignore_ascii_case(name) && header_value.trim() == value
    })
}

fn join_request(handle: RequestHandle) -> String {
    handle
        .join()
        .expect("server thread")
        .expect("captured request")
}

fn served(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> DaemonServedConfig {
    DaemonServedConfig::new(
        7,
        values
            .into_iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect(),
    )
}

fn spawn_delayed_json_response(delay: Duration) -> std::io::Result<(String, RequestHandle)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let base_url = format!("http://{}", listener.local_addr()?);
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        let request = read_http_request(&mut stream)?;
        thread::sleep(delay);
        let body = r#"{"revision":7,"config":{}}"#;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        )?;
        Ok(request)
    });
    Ok((base_url, handle))
}

fn spawn_stalled_json_body(delay: Duration) -> std::io::Result<(String, RequestHandle)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let base_url = format!("http://{}", listener.local_addr()?);
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        let request = read_http_request(&mut stream)?;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\nConnection: close\r\n\r\n"
        )?;
        stream.flush()?;
        thread::sleep(delay);
        Ok(request)
    });
    Ok((base_url, handle))
}

fn spawn_truncated_json_body() -> std::io::Result<(String, RequestHandle)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let base_url = format!("http://{}", listener.local_addr()?);
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept()?;
        let request = read_http_request(&mut stream)?;
        write!(
            stream,
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 64\r\nConnection: close\r\n\r\n{{}}"
        )?;
        Ok(request)
    });
    Ok((base_url, handle))
}

#[test]
#[serial_test::serial]
fn fetch_carries_bearer_and_parses_effective_config_envelope() {
    let _env_guard = EnvVarsGuard::set(&[
        ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None),
        ("GOBBY_AGENT_API_TOKEN", None),
    ]);
    let home = temp_home();
    fs::write(
        home.path().join(LOCAL_CLI_TOKEN_FILENAME),
        "effective-token",
    )
    .expect("write local CLI token");
    let (base_url, request) = spawn_json_response(
        r#"{"revision":7,"config":{"ai.embeddings.model":"nomic-embed-text","databases.postgres.dsn":"postgresql://daemon/gobby"}}"#,
    )
    .expect("spawn daemon");

    let mut daemon = daemon_mode_layers_at(&base_url, home.path()).expect("fetch effective config");

    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("nomic-embed-text")
    );
    assert_eq!(daemon.revision(), 7);
    let request = join_request(request);
    assert!(request.starts_with(&format!("GET {EFFECTIVE_CONFIG_PATH} HTTP/1.1")));
    assert!(has_header(
        &request,
        AUTHORIZATION_HEADER,
        "Bearer effective-token"
    ));
}

#[test]
#[serial_test::serial]
fn transport_and_non_success_statuses_are_sanitized_hard_errors() {
    let home = temp_home();

    let listener = TcpListener::bind("127.0.0.1:0").expect("reserve unused port");
    let unreachable = format!("http://{}", listener.local_addr().expect("local address"));
    drop(listener);
    let error =
        daemon_mode_layers_at(&unreachable, home.path()).expect_err("transport must hard fail");
    assert_eq!(
        error,
        EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Unreachable,
        }
    );
    assert!(!error.to_string().contains(&unreachable));

    let invalid_url = "not a daemon URL";
    let error =
        fetch_daemon_served_config_at_with_timeout(invalid_url, None, Duration::from_millis(20))
            .expect_err("invalid URL must hard fail");
    assert_eq!(
        error,
        EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Other,
        }
    );
    assert!(!error.to_string().contains(invalid_url));

    for status in [401, 500] {
        let (base_url, request) =
            spawn_json_response_with_status(status, "sensitive error body").expect("spawn daemon");
        let error =
            daemon_mode_layers_at(&base_url, home.path()).expect_err("status must hard fail");
        assert_eq!(error, EffectiveConfigError::HttpStatus { status });
        assert!(!error.to_string().contains("sensitive error body"));
        join_request(request);
    }
}

#[test]
fn loopback_timeout_is_bounded_and_categorized() {
    assert_eq!(EFFECTIVE_CONFIG_TIMEOUT, Duration::from_secs(5));

    let (base_url, request) =
        spawn_delayed_json_response(Duration::from_millis(20)).expect("spawn delayed daemon");
    fetch_daemon_served_config_at_with_timeout(&base_url, None, Duration::from_secs(2))
        .expect("response within timeout");
    join_request(request);

    let (base_url, request) =
        spawn_delayed_json_response(Duration::from_millis(100)).expect("spawn delayed daemon");
    let error =
        fetch_daemon_served_config_at_with_timeout(&base_url, None, Duration::from_millis(10))
            .expect_err("response after timeout must fail");
    assert_eq!(
        error,
        EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Timeout,
        }
    );
    assert!(!error.to_string().contains(&base_url));
    let _ = request.join().expect("delayed daemon thread");
}

#[test]
fn success_with_stalled_or_truncated_body_is_transport_failure() {
    let (base_url, request) =
        spawn_stalled_json_body(Duration::from_millis(100)).expect("spawn stalled daemon");
    let error =
        fetch_daemon_served_config_at_with_timeout(&base_url, None, Duration::from_millis(10))
            .expect_err("stalled response body must fail");
    assert_eq!(
        error,
        EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Timeout,
        }
    );
    join_request(request);

    let (base_url, request) = spawn_truncated_json_body().expect("spawn truncated daemon");
    let error =
        fetch_daemon_served_config_at_with_timeout(&base_url, None, Duration::from_millis(100))
            .expect_err("truncated response body must fail");
    assert!(matches!(error, EffectiveConfigError::Transport { .. }));
    join_request(request);
}

#[test]
#[serial_test::serial]
fn success_with_invalid_json_or_missing_envelope_is_protocol_failure() {
    let home = temp_home();

    for body in ["sensitive-response-body", "{}", r#"{"config":{}}"#] {
        let (base_url, request) = spawn_json_response(body).expect("spawn daemon");
        let error =
            daemon_mode_layers_at(&base_url, home.path()).expect_err("protocol must be strict");
        assert!(matches!(
            error,
            EffectiveConfigError::Protocol { status: 200, .. }
        ));
        let display = error.to_string();
        assert!(display.contains("HTTP 200"));
        assert!(!display.contains(body));
        join_request(request);
    }
}

#[test]
#[serial_test::serial]
fn served_secret_or_environment_references_are_contract_failures() {
    let cases = [
        (
            "ai.embeddings.model",
            "secret-marker embedding_model",
            "unresolved secret reference",
        ),
        (
            "ai.embeddings.query_prefix",
            "${PRIVATE_QUERY_PREFIX}",
            "unresolved environment reference",
        ),
    ];

    for (key, value, reason) in cases {
        assert!(
            is_machine_config_key(key),
            "the value-level backstop must be tested behind a machine-exportable key"
        );
        let values = BTreeMap::from([(key.to_string(), value.to_string())]);
        let error = validate_served_values(&values).expect_err("contract must be strict");
        assert!(matches!(error, EffectiveConfigError::Contract { .. }));
        let display = error.to_string();
        assert!(display.contains(key));
        assert!(display.contains(reason));
        assert!(!display.contains(value));
    }
}

#[test]
#[serial_test::serial]
fn malformed_local_yaml_after_fetch_keeps_daemon_mode_without_routing() {
    let _env_guard = EnvVarsGuard::set(&[
        ("GOBBY_MANAGED_EXECUTION_BOOTSTRAP", None),
        ("GOBBY_AGENT_API_TOKEN", None),
    ]);
    let home = temp_home();
    fs::write(home.path().join("grant-backed config"), "ai: [malformed")
        .expect("write malformed grant-backed yaml");
    let (base_url, request) =
        spawn_json_response(r#"{"revision":7,"config":{"ai.embeddings.model":"served-model"}}"#)
            .expect("spawn daemon");

    let mut daemon =
        daemon_mode_layers_at(&base_url, home.path()).expect("malformed local yaml is soft");

    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("served-model")
    );
    join_request(request);
}

#[test]
fn daemon_dsn_trims_available_value_and_propagates_failed_state() {
    let available = EffectiveConfigState::Available(served([(
        "databases.postgres.dsn",
        "  postgresql://daemon/gobby  ",
    )]));
    assert_eq!(
        daemon_dsn_from_state(&available).expect("available dsn"),
        Some("postgresql://daemon/gobby".to_string())
    );
    let blank = EffectiveConfigState::Available(served([("databases.postgres.dsn", "   ")]));
    assert_eq!(daemon_dsn_from_state(&blank).expect("blank dsn"), None);

    let failed = EffectiveConfigState::Failed(EffectiveConfigError::Protocol {
        status: 200,
        reason: "missing config envelope",
    });
    assert!(matches!(
        daemon_dsn_from_state(&failed),
        Err(EffectiveConfigError::Protocol { status: 200, .. })
    ));
}

#[test]
fn primary_factory_is_lazy_in_daemon_mode_and_once_in_standalone_mode() {
    let home = temp_home();
    fs::write(
        home.path().join("grant-backed config"),
        "ai.embeddings.model: standalone-model\n",
    )
    .expect("write standalone config");

    let daemon_calls = Cell::new(0);
    let mut daemon_source = ai_source_with_primary_from_layers(
        Ok(Some(served([("ai.embeddings.model", "daemon-model")]))),
        home.path(),
        || {
            daemon_calls.set(daemon_calls.get() + 1);
            Ok(NoPrimaryAiConfigSource)
        },
    )
    .expect("daemon source");
    assert_eq!(daemon_calls.get(), 0);
    assert_eq!(
        daemon_source.config_value("ai.embeddings.model").as_deref(),
        Some("daemon-model")
    );

    let standalone_calls = Cell::new(0);
    let mut standalone_source = ai_source_with_primary_from_layers(Ok(None), home.path(), || {
        standalone_calls.set(standalone_calls.get() + 1);
        Ok(NoPrimaryAiConfigSource)
    })
    .expect("standalone source");
    assert_eq!(standalone_calls.get(), 1);
    assert_eq!(
        standalone_source
            .config_value("ai.embeddings.model")
            .as_deref(),
        Some("standalone-model")
    );
}

#[test]
fn daemon_source_resolves_served_binding_and_embedding_settings_end_to_end() {
    let daemon = served([
        ("ai.embeddings.provider", "openai"),
        ("ai.embeddings.api_base", "https://daemon.example/v1"),
        ("ai.embeddings.model", "served-embed"),
        ("ai.embeddings.query_prefix", "search:"),
    ]);
    let mut source: AiConfigSource<DaemonOrPrimary<NoPrimaryAiConfigSource>> =
        AiConfigSource::with_primary(DaemonOrPrimary::Daemon(daemon));

    let binding = resolve_capability_binding(&mut source, AiCapability::Embed);
    assert_eq!(binding.provider.as_deref(), Some("openai"));
    assert_eq!(
        binding.api_base.as_deref(),
        Some("https://daemon.example/v1")
    );
    assert_eq!(binding.model.as_deref(), Some("served-embed"));

    let embedding =
        resolve_embedding_config_from_binding(&mut source, &binding).expect("embedding config");
    assert_eq!(embedding.model, "served-embed");
    assert_eq!(embedding.query_prefix.as_deref(), Some("search:"));
}

#[test]
fn daemon_mode_layers_surface_fetch_errors() {
    let calls = Cell::new(0);
    let error = daemon_mode_layers_for(|| {
        calls.set(calls.get() + 1);
        Err(EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Other,
        })
    })
    .expect_err("fetch errors stay typed");

    assert!(matches!(
        error,
        EffectiveConfigError::Transport {
            kind: EffectiveConfigTransportKind::Other
        }
    ));
    assert_eq!(calls.get(), 1);
}

#[test]
fn cached_state_is_cloneable() {
    let failed = EffectiveConfigState::Failed(EffectiveConfigError::Transport {
        kind: EffectiveConfigTransportKind::Unreachable,
    });
    assert!(matches!(
        failed.clone(),
        EffectiveConfigState::Failed(EffectiveConfigError::Transport { .. })
    ));

    let map = BTreeMap::from([("ai.embeddings.model".to_string(), "served".to_string())]);
    let state = EffectiveConfigState::Available(DaemonServedConfig::new(7, map));
    assert!(matches!(state.clone(), EffectiveConfigState::Available(_)));
}
