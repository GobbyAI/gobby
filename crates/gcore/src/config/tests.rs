use super::resolve::{
    EMBEDDING_DEFAULT_MODEL, EMBEDDING_DEFAULT_TIMEOUT_SECONDS, FALKORDB_DEFAULT_PORT,
};
use super::*;
use crate::provisioning::StandaloneConfig;
use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard, Once};

struct TestLogger {
    records: Mutex<Vec<String>>,
}

static TEST_LOGGER: TestLogger = TestLogger {
    records: Mutex::new(Vec::new()),
};
static TEST_LOGGER_INIT: Once = Once::new();

impl TestLogger {
    fn clear(&self) {
        self.lock_records().clear();
    }

    fn records(&self) -> Vec<String> {
        self.lock_records().clone()
    }

    fn lock_records(&self) -> std::sync::MutexGuard<'_, Vec<String>> {
        self.records
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl log::Log for TestLogger {
    fn enabled(&self, metadata: &log::Metadata<'_>) -> bool {
        metadata.level() <= log::Level::Warn
    }

    fn log(&self, record: &log::Record<'_>) {
        if self.enabled(record.metadata()) {
            self.lock_records()
                .push(format!("{}: {}", record.level(), record.args()));
        }
    }

    fn flush(&self) {}
}

fn capture_warn_logs<R>(f: impl FnOnce() -> R) -> (R, Vec<String>) {
    TEST_LOGGER_INIT.call_once(|| {
        log::set_logger(&TEST_LOGGER).expect("install test logger");
        log::set_max_level(log::LevelFilter::Warn);
    });
    TEST_LOGGER.clear();
    let result = f();
    (result, TEST_LOGGER.records())
}

// All process-environment mutation in this module must happen through
// EnvGuard, which holds TEST_ENV_LOCK for the full test scope.
struct EnvGuard {
    lock: MutexGuard<'static, ()>,
}

impl EnvGuard {
    fn new() -> Self {
        let guard = Self {
            lock: TEST_ENV_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        };
        guard.clear();
        guard
    }

    fn clear(&self) {
        let _held_env_lock = &self.lock;
        for key in [
            "GOBBY_FALKORDB_HOST",
            "GOBBY_FALKORDB_PORT",
            "GOBBY_FALKORDB_PASSWORD",
            "GOBBY_QDRANT_URL",
            "GOBBY_QDRANT_API_KEY",
            "GOBBY_INDEXING_RESPECT_GITIGNORE",
            "GOBBY_TEST_PRESENT",
            "GOBBY_TEST_MISSING",
        ] {
            // SAFETY: TEST_ENV_LOCK must guard every test environment mutation
            // in this crate. This loop only removes the fixed key list above.
            unsafe { std::env::remove_var(key) };
        }
    }

    fn set(&self, key: &str, value: &str) {
        let _held_env_lock = &self.lock;
        unsafe { std::env::set_var(key, value) };
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        self.clear();
    }
}

#[derive(Default)]
struct TestSource {
    values: HashMap<&'static str, String>,
    resolved_values: Vec<String>,
}

impl TestSource {
    fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
        Self {
            values: values
                .into_iter()
                .map(|(key, value)| (key, value.to_string()))
                .collect(),
            resolved_values: Vec::new(),
        }
    }

    fn with_raw_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
        Self {
            values: values
                .into_iter()
                .filter_map(|(key, value)| decode_config_value(value).map(|v| (key, v)))
                .collect(),
            resolved_values: Vec::new(),
        }
    }
}

impl ConfigSource for TestSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.resolved_values.push(value.to_string());
        if let Some(secret_name) = value.strip_prefix("$secret:") {
            return Ok(format!("resolved-{secret_name}"));
        }
        Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
    }
}

#[derive(Default)]
struct FailingResolveSource {
    values: HashMap<&'static str, String>,
    failing_values: Vec<String>,
}

impl FailingResolveSource {
    fn with_values_and_failures(
        values: impl IntoIterator<Item = (&'static str, &'static str)>,
        failing_values: impl IntoIterator<Item = &'static str>,
    ) -> Self {
        Self {
            values: values
                .into_iter()
                .map(|(key, value)| (key, value.to_string()))
                .collect(),
            failing_values: failing_values.into_iter().map(str::to_string).collect(),
        }
    }
}

impl ConfigSource for FailingResolveSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if self.failing_values.iter().any(|failing| failing == value) {
            return Err(anyhow::anyhow!("resolver failed"));
        }
        Ok(resolve_env_pattern(value)?.unwrap_or_else(|| value.to_string()))
    }
}

#[derive(Default)]
struct LayeredTestSource {
    store: TestSource,
    yaml: TestSource,
}

impl LayeredTestSource {
    fn with_layers(
        store_values: impl IntoIterator<Item = (&'static str, &'static str)>,
        yaml_values: impl IntoIterator<Item = (&'static str, &'static str)>,
    ) -> Self {
        Self {
            store: TestSource::with_values(store_values),
            yaml: TestSource::with_values(yaml_values),
        }
    }
}

impl ConfigSource for LayeredTestSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.store
            .config_value(key)
            .or_else(|| self.yaml.config_value(key))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        self.store.resolve_value(value)
    }
}

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
fn ai_routing_per_capability_precedence() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        (ai_keys::ROUTING, "daemon"),
        (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "direct"),
    ]);

    assert_eq!(
        resolve_capability_routing(&mut source, AiCapability::AudioTranscribe),
        AiRouting::Direct
    );

    let mut source = TestSource::with_values([(ai_keys::ROUTING, "off")]);
    assert_eq!(
        resolve_capability_routing(&mut source, AiCapability::VisionExtract),
        AiRouting::Off
    );

    let mut source = TestSource::default();
    assert_eq!(
        resolve_capability_routing(&mut source, AiCapability::TextGenerate),
        AiRouting::Auto
    );

    let mut source = TestSource::with_values([
        (ai_keys::TEXT_GENERATE_ROUTING, "unknown"),
        (ai_keys::ROUTING, "direct"),
    ]);
    assert_eq!(
        resolve_capability_routing(&mut source, AiCapability::TextGenerate),
        AiRouting::Direct
    );

    assert_eq!("daemon".parse::<AiRouting>().ok(), Some(AiRouting::Daemon));
    assert!("unknown".parse::<AiRouting>().is_err());
}

#[test]
fn ai_config_resolves_store_then_yaml_no_env() {
    let env = EnvGuard::new();
    env.set("GOBBY_TEST_PRESENT", "interpolated-text-model");

    let mut source = LayeredTestSource::with_layers(
        [
            (
                ai_keys::TEXT_GENERATE_API_BASE,
                "http://store-text:11434/v1",
            ),
            (ai_keys::TEXT_GENERATE_API_KEY, "$secret:TEXT_KEY"),
            (ai_keys::MAX_CONCURRENCY, "3"),
        ],
        [
            (ai_keys::TEXT_GENERATE_API_BASE, "http://yaml-text:11434/v1"),
            (ai_keys::TEXT_GENERATE_MODEL, "${GOBBY_TEST_PRESENT}"),
            (ai_keys::TEXT_GENERATE_API_KEY, "yaml-local-key"),
            (ai_keys::KEEP_ALIVE, "30s"),
        ],
    );

    let binding = resolve_capability_binding(&mut source, AiCapability::TextGenerate);
    let tuning = resolve_ai_tuning(&mut source);

    assert_eq!(
        binding.api_base.as_deref(),
        Some("http://store-text:11434/v1")
    );
    assert_eq!(binding.model.as_deref(), Some("interpolated-text-model"));
    assert_eq!(binding.api_key.as_deref(), Some("resolved-TEXT_KEY"));
    assert_eq!(tuning.max_concurrency, 3);
    assert_eq!(tuning.keep_alive.as_deref(), Some("30s"));

    let mut standalone_source = LayeredTestSource::with_layers(
        [],
        [
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://yaml-embedding:11434/v1",
            ),
            (ai_keys::EMBEDDINGS_API_KEY, "plaintext-local-key"),
        ],
    );
    let binding = resolve_capability_binding(&mut standalone_source, AiCapability::Embed);
    let embedding = resolve_embedding_config(&mut standalone_source).expect("embedding config");

    assert_eq!(
        binding.api_base.as_deref(),
        Some("http://yaml-embedding:11434/v1")
    );
    assert_eq!(binding.api_key.as_deref(), Some("plaintext-local-key"));
    assert_eq!(embedding.api_base, "http://yaml-embedding:11434/v1");

    let mut missing_env_source = LayeredTestSource::with_layers(
        [],
        [(ai_keys::TEXT_GENERATE_MODEL, "${GOBBY_TEST_MISSING}")],
    );
    let binding = resolve_capability_binding(&mut missing_env_source, AiCapability::TextGenerate);
    assert_eq!(binding.model, None);

    let tuning = resolve_ai_tuning(&mut TestSource::default());
    assert_eq!(tuning.max_concurrency, 1);
    assert!(tuning.keep_alive.is_none());
}

#[test]
#[serial_test::serial(config_log_capture)]
fn ai_binding_resolution_error_logs_config_key() {
    let raw_value = "$secret:MISSING_AI_KEY";
    let mut source = FailingResolveSource::with_values_and_failures(
        [(ai_keys::TEXT_GENERATE_API_BASE, raw_value)],
        [raw_value],
    );

    let (binding, warnings) =
        capture_warn_logs(|| resolve_capability_binding(&mut source, AiCapability::TextGenerate));

    assert!(binding.api_base.is_none());
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains(ai_keys::TEXT_GENERATE_API_BASE))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("resolver failed"));
    assert!(!matching[0].contains(raw_value));
}

#[test]
#[serial_test::serial(config_log_capture)]
fn env_override_resolution_error_logs_env_key() {
    let env = EnvGuard::new();
    let raw_value = "$secret:MISSING_QDRANT_KEY";
    env.set("GOBBY_QDRANT_API_KEY", raw_value);
    let mut source = FailingResolveSource::with_values_and_failures(
        [("databases.qdrant.url", "http://stored-qdrant:6333")],
        [raw_value],
    );

    let (qdrant, warnings) =
        capture_warn_logs(|| resolve_qdrant_config(&mut source).expect("qdrant config"));

    assert_eq!(qdrant.url.as_deref(), Some("http://stored-qdrant:6333"));
    assert!(qdrant.api_key.is_none());
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains("GOBBY_QDRANT_API_KEY"))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("resolver failed"));
    assert!(!matching[0].contains(raw_value));
}

#[test]
fn provider_and_translation_fields_resolve() {
    let _env = EnvGuard::new();
    let mut source = LayeredTestSource::with_layers(
        [
            (
                ai_keys::AUDIO_TRANSLATE_PROVIDER,
                "store-translate-provider",
            ),
            (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "fr"),
        ],
        [
            (ai_keys::AUDIO_TRANSLATE_PROVIDER, "yaml-translate-provider"),
            (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "de"),
            (ai_keys::AUDIO_TRANSCRIBE_LANGUAGE, "en"),
        ],
    );

    let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
    let transcribe = resolve_capability_binding(&mut source, AiCapability::AudioTranscribe);

    assert_eq!(
        translate.provider.as_deref(),
        Some("store-translate-provider")
    );
    assert_eq!(translate.target_lang.as_deref(), Some("fr"));
    assert!(translate.task.is_none());
    assert!(translate.language.is_none());

    assert_eq!(transcribe.language.as_deref(), Some("en"));
    assert!(transcribe.target_lang.is_none());
    assert!(
        ai_keys::all()
            .iter()
            .all(|key| !key.starts_with("ai.video."))
    );
    assert!(ai_keys::all().contains(&ai_keys::EMBEDDINGS_QUERY_PREFIX));
    assert!(ai_keys::all().contains(&ai_keys::EMBEDDINGS_DIM));
    assert!(ai_keys::all().contains(&ai_keys::EMBEDDINGS_TIMEOUT_SECONDS));
}

#[test]
fn audio_translate_inherits_transcribe_binding() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "direct"),
        (ai_keys::AUDIO_TRANSCRIBE_TRANSPORT, "daemon_native"),
        (ai_keys::AUDIO_TRANSCRIBE_API_BASE, "http://stt:8080/v1"),
        (ai_keys::AUDIO_TRANSCRIBE_API_KEY, "$secret:STT_KEY"),
        (ai_keys::AUDIO_TRANSCRIBE_MODEL, "whisper-large-v3"),
        (ai_keys::AUDIO_TRANSCRIBE_PROVIDER, "faster-whisper"),
        (ai_keys::AUDIO_TRANSCRIBE_TASK, "transcribe"),
        (ai_keys::AUDIO_TRANSCRIBE_LANGUAGE, "en"),
        (ai_keys::AUDIO_TRANSLATE_MODEL, "translate-override"),
        (ai_keys::AUDIO_TRANSLATE_PROVIDER, "translate-provider"),
        (ai_keys::AUDIO_TRANSLATE_TARGET_LANG, "es"),
    ]);

    let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
    let transcribe = resolve_capability_binding(&mut source, AiCapability::AudioTranscribe);

    assert_eq!(translate.routing, AiRouting::Direct);
    assert_eq!(translate.transport.as_deref(), Some("daemon_native"));
    assert_eq!(translate.api_base.as_deref(), Some("http://stt:8080/v1"));
    assert_eq!(translate.api_key.as_deref(), Some("resolved-STT_KEY"));
    assert_eq!(translate.model.as_deref(), Some("translate-override"));
    assert_eq!(translate.provider.as_deref(), Some("translate-provider"));
    assert_eq!(translate.target_lang.as_deref(), Some("es"));
    assert!(translate.task.is_none());
    assert!(translate.language.is_none());

    assert_eq!(transcribe.task.as_deref(), Some("transcribe"));
    assert_eq!(transcribe.language.as_deref(), Some("en"));
    assert!(transcribe.target_lang.is_none());

    let mut source = TestSource::with_values([
        (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
        (ai_keys::AUDIO_TRANSLATE_ROUTING, "off"),
    ]);
    let translate = resolve_capability_binding(&mut source, AiCapability::AudioTranslate);
    assert_eq!(translate.routing, AiRouting::Off);
}

#[test]
fn embedding_config_uses_ai_namespace() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        (embedding_keys::AI_API_BASE, "http://new-embedding:11434"),
        (embedding_keys::AI_MODEL, "new-model"),
        (embedding_keys::AI_API_KEY, "$secret:AI_KEY"),
        (embedding_keys::AI_QUERY_PREFIX, "new-query:"),
        (embedding_keys::AI_TIMEOUT_SECONDS, "12"),
    ]);

    let resolution = resolve_embedding_config_resolution(&mut source).expect("embedding config");
    let config = resolution.config;

    assert_eq!(resolution.namespace, embedding_keys::AI_NAMESPACE);
    assert_eq!(config.api_base, "http://new-embedding:11434");
    assert_eq!(config.model, "new-model");
    assert_eq!(config.api_key.as_deref(), Some("resolved-AI_KEY"));
    assert_eq!(config.query_prefix.as_deref(), Some("new-query:"));
    assert_eq!(config.timeout_seconds, 12);
}

#[test]
fn postgres_config_source_resolves_secrets() {
    let _env = EnvGuard::new();

    struct ConnectionLike {
        values: HashMap<&'static str, String>,
        secret_reads: usize,
    }

    struct PostgresConfigSource<'a> {
        conn: &'a mut ConnectionLike,
    }

    impl ConfigSource for PostgresConfigSource<'_> {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.conn.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            self.conn.secret_reads += 1;
            Ok(format!("secret::{value}"))
        }
    }

    let mut conn = ConnectionLike {
        values: HashMap::from([
            (
                embedding_keys::AI_API_BASE,
                "http://stored-embedding:11434".to_string(),
            ),
            (
                embedding_keys::AI_API_KEY,
                "$secret:OPENAI_API_KEY".to_string(),
            ),
        ]),
        secret_reads: 0,
    };
    let config = {
        let mut source = PostgresConfigSource { conn: &mut conn };
        resolve_embedding_config(&mut source).expect("embedding config")
    };

    assert_eq!(
        config.api_key.as_deref(),
        Some("secret::$secret:OPENAI_API_KEY")
    );
    assert_eq!(conn.secret_reads, 2);
}

#[test]
fn resolve_config_handles_json_encoded_store_values() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_raw_values([
        ("databases.falkordb.host", r#""json-falkor.local""#),
        ("databases.falkordb.port", r#""17001""#),
        ("databases.falkordb.password", r#""$secret:FALKOR_PASS""#),
        ("databases.qdrant.url", r#""http://json-qdrant:6333""#),
        ("databases.qdrant.api_key", r#"["alpha",1]"#),
        (
            embedding_keys::AI_API_BASE,
            r#""http://json-embedding:11434""#,
        ),
        (embedding_keys::AI_MODEL, r#"["model",1]"#),
    ]);

    let falkordb = resolve_falkordb_config(&mut source).expect("falkordb config");
    let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");
    let embedding = resolve_embedding_config(&mut source).expect("embedding config");

    assert_eq!(falkordb.host, "json-falkor.local");
    assert_eq!(falkordb.port, 17001);
    assert_eq!(falkordb.password.as_deref(), Some("resolved-FALKOR_PASS"));
    assert_eq!(qdrant.url.as_deref(), Some("http://json-qdrant:6333"));
    assert_eq!(qdrant.api_key.as_deref(), Some(r#"["alpha",1]"#));
    assert_eq!(embedding.api_base, "http://json-embedding:11434");
    assert_eq!(embedding.model, r#"["model",1]"#);
}

#[test]
fn qdrant_and_embedding_resolution_order() {
    {
        let env = EnvGuard::new();
        env.set("GOBBY_QDRANT_API_KEY", "env-qdrant-key");
        let mut source = TestSource::with_values([
            ("databases.qdrant.url", "http://stored-qdrant:6333"),
            ("databases.qdrant.api_key", "stored-qdrant-key"),
            (
                embedding_keys::AI_API_BASE,
                "http://stored-embedding:11434/v1",
            ),
            (embedding_keys::AI_MODEL, "stored-embedding-model"),
            (embedding_keys::AI_API_KEY, "$secret:EMBEDDING_KEY"),
            (embedding_keys::AI_QUERY_PREFIX, "stored-query-prefix:"),
        ]);

        let qdrant = resolve_qdrant_config(&mut source).expect("qdrant config");
        let embedding = resolve_embedding_config(&mut source).expect("embedding config");

        assert_eq!(qdrant.url.as_deref(), Some("http://stored-qdrant:6333"));
        assert_eq!(qdrant.api_key.as_deref(), Some("env-qdrant-key"));
        assert_eq!(embedding.api_base, "http://stored-embedding:11434/v1");
        assert_eq!(embedding.model, "stored-embedding-model");
        assert_eq!(embedding.api_key.as_deref(), Some("resolved-EMBEDDING_KEY"));
        assert_eq!(
            embedding.query_prefix.as_deref(),
            Some("stored-query-prefix:")
        );
        assert_eq!(embedding.timeout_seconds, EMBEDDING_DEFAULT_TIMEOUT_SECONDS);
    }

    let _env = EnvGuard::new();
    let mut default_source = TestSource::with_values([(
        embedding_keys::AI_API_BASE,
        "http://stored-embedding:11434/v1",
    )]);
    let default_embedding =
        resolve_embedding_config(&mut default_source).expect("embedding config");

    assert_eq!(default_embedding.model, EMBEDDING_DEFAULT_MODEL);
    assert!(default_embedding.query_prefix.is_none());
    assert_eq!(
        default_embedding.timeout_seconds,
        EMBEDDING_DEFAULT_TIMEOUT_SECONDS
    );
    assert!(resolve_qdrant_config(&mut TestSource::default()).is_none());
}

#[test]
fn indexing_config_defaults_to_respecting_gitignore() {
    let _env = EnvGuard::new();
    let mut source = TestSource::default();

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert!(indexing.respect_gitignore);
}

#[test]
fn indexing_config_resolves_standalone_yaml_values() {
    let _env = EnvGuard::new();
    for (raw, expected) in [("true", true), ("false", false)] {
        let standalone =
            StandaloneConfig::from_yaml_str(&format!("indexing:\n  respect_gitignore: {raw}\n"))
                .expect("standalone config");
        let mut source =
            LayeredConfigSource::<TestSource, StandaloneConfig>::new(None, Some(standalone));

        let indexing = resolve_indexing_config(&mut source).expect("indexing config");

        assert_eq!(indexing.respect_gitignore, expected);
    }
}

#[test]
fn indexing_config_resolves_config_store_values_before_yaml() {
    let _env = EnvGuard::new();
    for (raw, yaml, expected) in [("false", "true", false), ("true", "false", true)] {
        let store = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, raw)]);
        let standalone =
            StandaloneConfig::from_yaml_str(&format!("indexing:\n  respect_gitignore: {yaml}\n"))
                .expect("standalone config");
        let mut source = LayeredConfigSource::new(Some(store), Some(standalone));

        let indexing = resolve_indexing_config(&mut source).expect("indexing config");

        assert_eq!(indexing.respect_gitignore, expected);
    }
}

#[test]
fn indexing_config_invalid_boolean_warns_and_uses_default() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([(INDEXING_RESPECT_GITIGNORE_KEY, "sometimes")]);

    let (indexing, warnings) =
        capture_warn_logs(|| resolve_indexing_config(&mut source).expect("indexing config"));

    assert!(indexing.respect_gitignore);
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains(INDEXING_RESPECT_GITIGNORE_KEY))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("invalid boolean"));
    assert!(matching[0].contains("using default true"));
    assert!(!matching[0].contains("sometimes"));
}

#[test]
fn indexing_config_env_overrides_config_sources() {
    let env = EnvGuard::new();
    env.set("GOBBY_INDEXING_RESPECT_GITIGNORE", "false");
    let standalone = StandaloneConfig::from_yaml_str("indexing:\n  respect_gitignore: true\n")
        .expect("standalone config");
    let store = TestSource::with_raw_values([(INDEXING_RESPECT_GITIGNORE_KEY, "true")]);
    let mut source = LayeredConfigSource::new(Some(store), Some(standalone));

    let indexing = resolve_indexing_config(&mut source).expect("indexing config");

    assert!(!indexing.respect_gitignore);
}

#[test]
fn indexing_config_invalid_environment_boolean_warns_and_uses_default() {
    let env = EnvGuard::new();
    env.set("GOBBY_INDEXING_RESPECT_GITIGNORE", "sometimes");
    let mut source = TestSource::with_values([(INDEXING_RESPECT_GITIGNORE_KEY, "false")]);

    let (indexing, warnings) =
        capture_warn_logs(|| resolve_indexing_config(&mut source).expect("indexing config"));

    assert!(indexing.respect_gitignore);
    let matching = warnings
        .iter()
        .filter(|warning| warning.contains("GOBBY_INDEXING_RESPECT_GITIGNORE"))
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1, "{warnings:?}");
    assert!(matching[0].contains("invalid boolean"));
    assert!(matching[0].contains("using default true"));
    assert!(!matching[0].contains("sometimes"));
}

#[test]
fn invalid_embedding_timeout_uses_default() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        (
            embedding_keys::AI_API_BASE,
            "http://stored-embedding:11434/v1",
        ),
        (embedding_keys::AI_TIMEOUT_SECONDS, "not-a-number"),
    ]);

    let embedding = resolve_embedding_config(&mut source).expect("embedding config");

    assert_eq!(embedding.timeout_seconds, EMBEDDING_DEFAULT_TIMEOUT_SECONDS);
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

#[test]
fn embedding_keys_centralized() {
    if std::env::var("RUN_SLOW_TESTS").is_err() {
        eprintln!("skipping slow workspace embedding key scan; set RUN_SLOW_TESTS=1 to run");
        return;
    }

    let workspace = workspace_root();
    let offenders = embedding_key_literal_offenders(&workspace.join("crates"));

    assert!(
        offenders.is_empty(),
        "embedding config key literals must stay in gobby_core::config::embedding_keys: {offenders:?}"
    );
}

#[test]
fn ci_guard_rejects_stray_literal() {
    let dir = tempfile::tempdir().expect("tempdir");
    let src = dir.path().join("src");
    std::fs::create_dir_all(&src).expect("create src");
    std::fs::write(
        src.join("bad.rs"),
        format!(r#"const BAD: &str = "{}";"#, embedding_keys::AI_API_BASE),
    )
    .expect("write bad source");

    let offenders = embedding_key_literal_offenders(dir.path());

    assert_eq!(offenders.len(), 1);
    assert!(offenders[0].ends_with("bad.rs"));
}

fn workspace_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(std::path::Path::parent)
        .expect("workspace root")
        .to_path_buf()
}

fn embedding_key_literal_offenders(root: &std::path::Path) -> Vec<std::path::PathBuf> {
    let mut offenders = Vec::new();
    visit_embedding_key_literal_sources(root, &mut offenders);
    offenders
}

fn visit_embedding_key_literal_sources(
    path: &std::path::Path,
    offenders: &mut Vec<std::path::PathBuf>,
) {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(_) => return,
    };
    for entry in entries {
        let entry = entry.expect("directory entry");
        let path = entry.path();
        if path.is_dir() {
            if should_skip_embedding_key_scan_dir(&path) {
                continue;
            }
            visit_embedding_key_literal_sources(&path, offenders);
            continue;
        }
        if path.extension().and_then(|ext| ext.to_str()) != Some("rs") {
            continue;
        }
        if embedding_key_literal_allowed_path(&path) {
            continue;
        }
        let source = std::fs::read_to_string(&path).expect("read source file");
        if guarded_embedding_keys()
            .iter()
            .any(|key| source.contains(key.as_str()))
        {
            offenders.push(path);
        }
    }
}

fn should_skip_embedding_key_scan_dir(path: &std::path::Path) -> bool {
    matches!(
        path.file_name().and_then(|name| name.to_str()),
        Some(
            ".git"
                | "target"
                | "node_modules"
                | "dist"
                | "build"
                | ".venv"
                | "venv"
                | "__pycache__"
        )
    )
}

fn guarded_embedding_keys() -> Vec<String> {
    vec![
        embedding_keys::AI_PROVIDER,
        embedding_keys::AI_API_BASE,
        embedding_keys::AI_MODEL,
        embedding_keys::AI_API_KEY,
        embedding_keys::AI_QUERY_PREFIX,
        embedding_keys::AI_DIM,
        embedding_keys::AI_TIMEOUT_SECONDS,
    ]
    .into_iter()
    .map(str::to_string)
    .collect::<Vec<_>>()
}

fn embedding_key_literal_allowed_path(path: &std::path::Path) -> bool {
    let path = path.to_string_lossy();
    path.ends_with("crates/gcore/src/config/types.rs")
        || path.ends_with("tests.rs")
        || path.contains("/tests/")
}
