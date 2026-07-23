use super::*;
use crate::provisioning::StandaloneConfig;
use std::collections::BTreeMap;
use std::fs;

#[derive(Debug)]
struct PrimarySource;

impl ConfigSource for PrimarySource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        Some(format!("primary:{key}"))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        Ok(format!("primary:{value}"))
    }
}

#[test]
fn daemon_served_config_filters_routing_keys_and_returns_stored_values() {
    let mut source = DaemonServedConfig::new(BTreeMap::from([
        ("ai.routing".to_string(), "daemon".to_string()),
        ("ai.text_generate.routing".to_string(), "direct".to_string()),
        (
            "ai.embeddings.api_base".to_string(),
            "http://daemon.example/v1".to_string(),
        ),
    ]));

    assert_eq!(source.config_value("ai.routing"), None);
    assert_eq!(source.config_value("ai.text_generate.routing"), None);
    assert_eq!(
        source.config_value("ai.embeddings.api_base").as_deref(),
        Some("http://daemon.example/v1")
    );
}

#[test]
fn daemon_served_config_resolves_values_verbatim() {
    let mut source = DaemonServedConfig::default();

    assert_eq!(
        source
            .resolve_value("${CLIENT_ENV_MUST_NOT_EXPAND}")
            .expect("daemon value is already validated"),
        "${CLIENT_ENV_MUST_NOT_EXPAND}"
    );
}

#[test]
fn daemon_or_primary_delegates_to_the_active_source() {
    let mut daemon =
        DaemonOrPrimary::<PrimarySource>::Daemon(DaemonServedConfig::new(BTreeMap::from([(
            "ai.embeddings.model".to_string(),
            "daemon-model".to_string(),
        )])));
    assert_eq!(
        daemon.config_value("ai.embeddings.model").as_deref(),
        Some("daemon-model")
    );
    assert_eq!(
        daemon
            .resolve_value("daemon-value")
            .expect("daemon resolution"),
        "daemon-value"
    );

    let mut primary = DaemonOrPrimary::Primary(PrimarySource);
    assert_eq!(
        primary.config_value("ai.embeddings.model").as_deref(),
        Some("primary:ai.embeddings.model")
    );
    assert_eq!(
        primary
            .resolve_value("primary-value")
            .expect("primary resolution"),
        "primary:primary-value"
    );
}

#[test]
fn routing_overrides_drop_non_routing_and_switch_run_values() {
    let config = StandaloneConfig::new(BTreeMap::from([
        ("ai.routing".to_string(), "daemon".to_string()),
        ("ai.text_generate.routing".to_string(), "direct".to_string()),
        (
            "ai.embeddings.provider".to_string(),
            "openai-compatible".to_string(),
        ),
        ("ai.embeddings.switch_run".to_string(), "42".to_string()),
    ]));

    let routing = routing_overrides_only(config);

    assert_eq!(
        routing.values(),
        &BTreeMap::from([
            ("ai.routing".to_string(), "daemon".to_string()),
            ("ai.text_generate.routing".to_string(), "direct".to_string(),),
        ])
    );
}

#[test]
fn raw_yaml_skips_text_generation_derivation() {
    let yaml = "ai:\n  embeddings:\n    provider: openai-compatible\n";

    let raw = StandaloneConfig::from_yaml_str_raw(yaml).expect("parse raw config");
    let derived = StandaloneConfig::from_yaml_str(yaml).expect("parse derived config");

    assert_eq!(raw.get(ai_keys::TEXT_GENERATE_ROUTING), None);
    assert_eq!(derived.get(ai_keys::TEXT_GENERATE_ROUTING), Some("direct"));
}

#[test]
fn raw_file_routing_survives_routing_only_filter() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("gcore.yaml");
    fs::write(
        &path,
        "ai:\n  routing: daemon\n  embeddings:\n    provider: ollama\n  text_generate:\n    routing: direct\n",
    )
    .expect("write config");

    let raw = StandaloneConfig::read_raw_at(&path)
        .expect("read raw config")
        .expect("config present");
    let routing = routing_overrides_only(raw);

    assert_eq!(routing.get("ai.routing"), Some("daemon"));
    assert_eq!(routing.get(ai_keys::TEXT_GENERATE_ROUTING), Some("direct"));
    assert_eq!(routing.get("ai.embeddings.provider"), None);
}
