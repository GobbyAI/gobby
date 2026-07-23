use std::cell::Cell;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::net::TcpListener;
use std::path::Path;
use std::sync::MutexGuard;

use tempfile::TempDir;

use super::*;
use crate::ai_context::{AiConfigSource, NoPrimaryAiConfigSource};
use crate::config::{
    AiCapability, ConfigSource, DaemonOrPrimary, DaemonServedConfig, TEST_ENV_LOCK,
    resolve_capability_binding, resolve_embedding_config_from_binding,
};
use crate::local_token::{AUTHORIZATION_HEADER, LOCAL_CLI_TOKEN_FILENAME};
use crate::test_http::{RequestHandle, spawn_json_response, spawn_json_response_with_status};

struct EnvGuard {
    _lock: MutexGuard<'static, ()>,
    original_gobby_home: Option<OsString>,
}

impl EnvGuard {
    fn set_gobby_home(home: &Path, token: &str) -> Self {
        let lock = TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        fs::write(home.join(LOCAL_CLI_TOKEN_FILENAME), token).expect("write local CLI token");
        let original_gobby_home = std::env::var_os("GOBBY_HOME");
        // SAFETY: TEST_ENV_LOCK serializes every test mutation of GOBBY_HOME.
        unsafe { std::env::set_var("GOBBY_HOME", home) };
        Self {
            _lock: lock,
            original_gobby_home,
        }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        // SAFETY: this guard still owns TEST_ENV_LOCK while restoring GOBBY_HOME.
        unsafe {
            match &self.original_gobby_home {
                Some(value) => std::env::set_var("GOBBY_HOME", value),
                None => std::env::remove_var("GOBBY_HOME"),
            }
        }
    }
}

fn temp_home() -> TempDir {
    tempfile::tempdir().expect("temp gobby home")
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
        values
            .into_iter()
            .map(|(key, value)| (key.to_string(), value.to_string()))
            .collect(),
    )
}

#[test]
fn fetch_carries_bearer_and_parses_effective_config_envelope() {
    let home = temp_home();
    let _env = EnvGuard::set_gobby_home(home.path(), "effective-token");
    let (base_url, request) = spawn_json_response(
        r#"{"config":{"ai.embeddings.model":"nomic-embed-text","databases.postgres.dsn":"postgresql://daemon/gobby"}}"#,
    )
    .expect("spawn daemon");

    let (mut daemon, routing) = daemon_mode_layers_at(&base_url, home.path())
        .expect("fetch effective config")
        .expect("daemon mode");

    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("nomic-embed-text")
    );
    assert!(routing.is_none());
    let request = join_request(request);
    assert!(request.starts_with(&format!("GET {EFFECTIVE_CONFIG_PATH} HTTP/1.1")));
    assert!(has_header(
        &request,
        AUTHORIZATION_HEADER,
        "Bearer effective-token"
    ));
}

#[test]
fn transport_and_non_success_statuses_are_unavailable() {
    let home = temp_home();
    let _env = EnvGuard::set_gobby_home(home.path(), "effective-token");

    let listener = TcpListener::bind("127.0.0.1:0").expect("reserve unused port");
    let unreachable = format!("http://{}", listener.local_addr().expect("local address"));
    drop(listener);
    assert_eq!(
        daemon_mode_layers_at(&unreachable, home.path()).expect("transport is absence"),
        None
    );

    for status in [401, 500] {
        let (base_url, request) =
            spawn_json_response_with_status(status, "sensitive error body").expect("spawn daemon");
        assert_eq!(
            daemon_mode_layers_at(&base_url, home.path()).expect("status is absence"),
            None
        );
        join_request(request);
    }
}

#[test]
fn success_with_invalid_json_or_missing_envelope_is_protocol_failure() {
    let home = temp_home();
    let _env = EnvGuard::set_gobby_home(home.path(), "effective-token");

    for body in ["sensitive-response-body", "{}"] {
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
fn served_secret_or_environment_references_are_contract_failures() {
    let home = temp_home();
    let _env = EnvGuard::set_gobby_home(home.path(), "effective-token");
    let cases = [
        (
            "databases.postgres.dsn",
            "postgresql://user:$secret:DB_PASSWORD@host/gobby",
        ),
        (
            "ai.embeddings.api_base",
            "https://${PRIVATE_HOST}/embeddings",
        ),
    ];

    for (key, value) in cases {
        let body = serde_json::json!({"config": {key: value}}).to_string();
        let (base_url, request) = spawn_json_response(body).expect("spawn daemon");
        let error =
            daemon_mode_layers_at(&base_url, home.path()).expect_err("contract must be strict");
        assert!(matches!(error, EffectiveConfigError::Contract { .. }));
        let display = error.to_string();
        assert!(display.contains(key));
        assert!(!display.contains(value));
        join_request(request);
    }
}

#[test]
fn malformed_local_yaml_after_fetch_keeps_daemon_mode_without_routing() {
    let home = temp_home();
    let _env = EnvGuard::set_gobby_home(home.path(), "effective-token");
    fs::write(home.path().join("gcore.yaml"), "ai: [malformed")
        .expect("write malformed gcore yaml");
    let (base_url, request) =
        spawn_json_response(r#"{"config":{"ai.embeddings.model":"served-model"}}"#)
            .expect("spawn daemon");

    let (mut daemon, routing) = daemon_mode_layers_at(&base_url, home.path())
        .expect("malformed local yaml is soft")
        .expect("daemon mode remains selected");

    assert!(routing.is_none());
    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("served-model")
    );
    join_request(request);
}

#[test]
fn daemon_dsn_trims_available_value_and_propagates_failed_state() {
    let available = EffectiveConfigState::Available((
        served([("databases.postgres.dsn", "  postgresql://daemon/gobby  ")]),
        None,
    ));
    assert_eq!(
        daemon_dsn_from_state(&available).expect("available dsn"),
        Some("postgresql://daemon/gobby".to_string())
    );
    let blank =
        EffectiveConfigState::Available((served([("databases.postgres.dsn", "   ")]), None));
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
        home.path().join("gcore.yaml"),
        "ai.embeddings.model: standalone-model\n",
    )
    .expect("write standalone config");

    let daemon_calls = Cell::new(0);
    let mut daemon_source = ai_source_with_primary_from_layers(
        Ok(Some((
            served([("ai.embeddings.model", "daemon-model")]),
            None,
        ))),
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
        AiConfigSource::with_primary(DaemonOrPrimary::Daemon(daemon), None);

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
fn cached_state_is_cloneable() {
    let state = EffectiveConfigState::Unavailable;
    assert!(matches!(state.clone(), EffectiveConfigState::Unavailable));

    let map = BTreeMap::from([("ai.embeddings.model".to_string(), "served".to_string())]);
    let state =
        EffectiveConfigState::Available((DaemonServedConfig::new(map), Some(Default::default())));
    assert!(matches!(
        state.clone(),
        EffectiveConfigState::Available((_, Some(_)))
    ));
}
