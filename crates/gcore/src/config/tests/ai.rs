use super::super::resolve::{EMBEDDING_DEFAULT_MODEL, EMBEDDING_DEFAULT_TIMEOUT_SECONDS};
use super::*;

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
fn text_generate_resolves_verify_overrides() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([
        (ai_keys::TEXT_GENERATE_MODEL, "generate-model"),
        (ai_keys::TEXT_GENERATE_API_KEY, "generate-key"),
        (ai_keys::TEXT_GENERATE_VERIFY_PROFILE, "feature_low"),
        (ai_keys::TEXT_GENERATE_VERIFY_MODEL, "verify-model"),
        (ai_keys::TEXT_GENERATE_VERIFY_API_KEY, "verify-key"),
    ]);

    let binding = resolve_capability_binding(&mut source, AiCapability::TextGenerate);
    assert_eq!(binding.model.as_deref(), Some("generate-model"));
    assert_eq!(binding.api_key.as_deref(), Some("generate-key"));
    assert_eq!(binding.verify_profile.as_deref(), Some("feature_low"));
    assert_eq!(binding.verify_model.as_deref(), Some("verify-model"));
    assert_eq!(binding.verify_api_key.as_deref(), Some("verify-key"));

    // Verify keys are TextGenerate-only; other capabilities never carry them.
    let embed = resolve_capability_binding(&mut source, AiCapability::Embed);
    assert_eq!(embed.verify_profile, None);
    assert_eq!(embed.verify_model, None);
    assert_eq!(embed.verify_api_key, None);
}

#[test]
fn text_generate_verify_overrides_absent_by_default() {
    let _env = EnvGuard::new();
    let mut source = TestSource::with_values([(ai_keys::TEXT_GENERATE_MODEL, "generate-model")]);
    let binding = resolve_capability_binding(&mut source, AiCapability::TextGenerate);
    assert_eq!(binding.verify_profile, None);
    assert_eq!(binding.verify_model, None);
    assert_eq!(binding.verify_api_key, None);
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
