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
fn config_source_resolves_falkordb_when_password_key_absent() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([("databases.falkordb.host", "falkor.local")]);

    let config = resolve_falkordb_config(&mut source).expect("falkordb config");

    assert_eq!(config.host, "falkor.local");
    assert_eq!(config.password, None);
}

#[test]
fn env_only_source_rejects_secret_markers() {
    let _env = EnvGuard::new();
    let mut source = EnvOnlySource;
    let marker = crate::config::secret_marker_prefix() + "FALKOR_PASS";

    let error = source
        .resolve_value(&marker)
        .expect_err("secret marker must fail typed");

    assert!(error.to_string().contains("grant-issuance"));
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
