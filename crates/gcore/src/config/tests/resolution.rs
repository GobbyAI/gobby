use super::super::resolve::FALKORDB_DEFAULT_PORT;
use super::*;

#[test]
fn decode_config_value_handles_json_and_plain() {
    assert_eq!(
        decode_config_value("\"http://host:7474\""),
        Some("http://host:7474".to_string())
    );
    assert_eq!(
        decode_config_value(r#"["alpha",1,true]"#),
        Some(r#"["alpha",1,true]"#.to_string())
    );
    assert_eq!(
        decode_config_value(r#"{"host":"falkor.local","port":16379}"#),
        Some(r#"{"host":"falkor.local","port":16379}"#.to_string())
    );
    assert_eq!(decode_config_value("42"), Some("42".to_string()));
    assert_eq!(decode_config_value("true"), Some("true".to_string()));
    assert_eq!(
        decode_config_value("http://plain:7474"),
        Some("http://plain:7474".to_string())
    );
    assert_eq!(decode_config_value("null"), None);
}

#[test]
fn resolve_env_pattern_with_defaults() {
    let env = EnvGuard::new();
    env.set("GOBBY_TEST_PRESENT", "present-value");

    assert_eq!(
        resolve_env_pattern("${GOBBY_TEST_PRESENT}").unwrap(),
        Some("present-value".to_string())
    );
    assert_eq!(
        resolve_env_pattern("prefix-${GOBBY_TEST_PRESENT}-suffix").unwrap(),
        Some("prefix-present-value-suffix".to_string())
    );
    assert_eq!(
        resolve_env_pattern("${GOBBY_TEST_MISSING:-fallback}").unwrap(),
        Some("fallback".to_string())
    );
    assert_eq!(resolve_env_pattern("${GOBBY_TEST_MISSING}").unwrap(), None);
    assert_eq!(
        resolve_env_pattern("plain-value").unwrap(),
        Some("plain-value".to_string())
    );
}

#[test]
fn env_overrides_config_store() {
    let env = EnvGuard::new();
    env.set("GOBBY_FALKORDB_HOST", "env-falkor.local");
    env.set("GOBBY_FALKORDB_PORT", "17000");
    env.set("GOBBY_FALKORDB_PASSWORD", "env-pass");
    env.set("GOBBY_QDRANT_URL", "http://env-qdrant:6333");
    env.set("GOBBY_QDRANT_API_KEY", "env-qdrant-key");

    let mut source = TestSource::with_values([
        ("databases.falkordb.host", "stored-falkor.local"),
        ("databases.falkordb.port", "16000"),
        ("databases.falkordb.password", "stored-pass"),
        ("databases.qdrant.url", "http://stored-qdrant:6333"),
        ("databases.qdrant.api_key", "stored-qdrant-key"),
    ]);

    let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
    let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");

    assert_eq!(falkordb.host, "env-falkor.local");
    assert_eq!(falkordb.port, 17000);
    assert_eq!(falkordb.password.as_deref(), Some("env-pass"));
    assert_eq!(qdrant.url.as_deref(), Some("http://env-qdrant:6333"));
    assert_eq!(qdrant.api_key.as_deref(), Some("env-qdrant-key"));
}

#[test]
#[serial_test::serial(config_log_capture)]
fn invalid_falkordb_env_port_warns_and_uses_default() {
    let env = EnvGuard::new();
    env.set("GOBBY_FALKORDB_HOST", "env-falkor.local");
    env.set("GOBBY_FALKORDB_PORT", "notaport");
    let mut source = TestSource::with_values([("databases.falkordb.port", "17000")]);

    let (falkordb, warnings) =
        capture_warn_logs(|| resolve_falkordb_config(&mut source).expect("falkordb config"));

    assert_eq!(falkordb.host, "env-falkor.local");
    assert_eq!(falkordb.port, FALKORDB_DEFAULT_PORT);
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains("GOBBY_FALKORDB_PORT"))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("invalid port"));
    assert!(matching[0].contains(&format!("using default {FALKORDB_DEFAULT_PORT}")));
    assert!(!matching[0].contains("notaport"));
}

#[test]
fn falkordb_password_resolves_current_config_key() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        ("databases.falkordb.host", "stored-falkor.local"),
        ("databases.falkordb.port", "16000"),
        ("databases.falkordb.password", "stored-pass"),
    ]);

    let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");

    assert_eq!(falkordb.host, "stored-falkor.local");
    assert_eq!(falkordb.port, 16000);
    assert_eq!(falkordb.password.as_deref(), Some("stored-pass"));
}

#[test]
fn config_source_handles_secrets() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        ("databases.falkordb.host", "falkor.local"),
        ("databases.falkordb.password", "$secret:FALKOR_PASS"),
    ]);

    let config = resolve_falkordb_config(&mut source).expect("falkordb config");

    assert_eq!(config.password.as_deref(), Some("resolved-FALKOR_PASS"));
    assert!(
        source
            .resolved_values
            .iter()
            .any(|value| value == "$secret:FALKOR_PASS")
    );
}

#[test]
fn env_only_source_rejects_secret_patterns() {
    let _env = EnvGuard::new();
    let mut source = EnvOnlySource;

    let error = source
        .resolve_value("$secret:FALKOR_PASS")
        .expect_err("secret resolution should require a datastore-backed source");

    assert!(error.to_string().contains("secret resolution"));
}

#[test]
fn falkordb_config_has_no_domain_graph_name() {
    let config = FalkorConfig {
        host: "falkor.local".to_string(),
        port: 16379,
        password: None,
    };

    // FalkorConfig stays connection-only; graph selection is supplied by consumers.
    assert!(!format!("{config:?}").contains("graph"));
}

#[test]
fn qdrant_config_has_no_domain_collection_prefix() {
    let config = QdrantConfig {
        url: Some("http://qdrant:6333".to_string()),
        api_key: None,
    };

    assert!(!format!("{config:?}").contains("collection"));
}
