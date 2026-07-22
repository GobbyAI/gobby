use super::common::*;

const PROVIDER_API_KEY_ENV_VARS: [&str; 4] = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
];

struct ProviderApiKeyEnvGuard {
    _lock: std::sync::MutexGuard<'static, ()>,
    previous: Vec<(&'static str, Option<std::ffi::OsString>)>,
}

impl ProviderApiKeyEnvGuard {
    fn new() -> Self {
        let lock = TEST_ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let previous = PROVIDER_API_KEY_ENV_VARS
            .into_iter()
            .map(|name| (name, std::env::var_os(name)))
            .collect();
        let guard = Self {
            _lock: lock,
            previous,
        };
        for name in PROVIDER_API_KEY_ENV_VARS {
            // SAFETY: process-environment mutation is serialized by TEST_ENV_LOCK,
            // which the guard holds until all original values are restored.
            unsafe { std::env::remove_var(name) };
        }
        guard
    }

    fn set(&self, name: &str, value: &str) {
        let _held_env_lock = &self._lock;
        // SAFETY: ProviderApiKeyEnvGuard holds TEST_ENV_LOCK for its lifetime.
        unsafe { std::env::set_var(name, value) };
    }
}

impl Drop for ProviderApiKeyEnvGuard {
    fn drop(&mut self) {
        // SAFETY: the guard still owns TEST_ENV_LOCK while restoring every key.
        unsafe {
            for (name, value) in &self.previous {
                match value {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

// ----- tier -> profile mapping -----------------------------------------------

#[test]
fn tier_profile_mapping_is_fixed_with_aggregate_override() {
    assert_eq!(
        profile_for_tier(GenerationTier::Standard, None).as_str(),
        FEATURE_LOW
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Module, None).as_str(),
        FEATURE_MID
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, None).as_str(),
        FEATURE_HIGH
    );

    // Override applies to Aggregate only.
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, Some("feature_custom")).as_str(),
        "feature_custom"
    );
    assert_eq!(
        profile_for_tier(GenerationTier::Module, Some("feature_custom")).as_str(),
        FEATURE_MID
    );

    // Blank override falls back to the default high tier.
    assert_eq!(
        profile_for_tier(GenerationTier::Aggregate, Some("   ")).as_str(),
        FEATURE_HIGH
    );
}

// ----- standalone Direct profile resolution ----------------------------------

struct MapSource {
    values: BTreeMap<String, String>,
}

impl MapSource {
    fn new() -> Self {
        Self {
            values: BTreeMap::new(),
        }
    }

    fn with(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.values.insert(key.into(), value.into());
        self
    }
}

impl ConfigSource for MapSource {
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.values.get(key).cloned()
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        // Standalone behavior: expand ${ENV} patterns, pass plaintext through.
        crate::config::resolve_env_pattern(value)?
            .ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
    }
}

#[test]
fn profile_field_prefers_profile_key_then_base_fallback() {
    let mut source = MapSource::new()
        .with(
            ai_keys::text_generate_profile_key("feature_high", ai_keys::PROFILE_MODEL),
            "high-model",
        )
        .with(ai_keys::TEXT_GENERATE_MODEL, "base-model")
        .with(ai_keys::TEXT_GENERATE_API_BASE, "http://base:1234/v1");

    let target = resolve_direct_generation_target(&mut source, "feature_high");
    // Profile-specific model wins.
    assert_eq!(target.model.as_deref(), Some("high-model"));
    // api_base falls back to the base text_generate binding.
    assert_eq!(target.api_base.as_deref(), Some("http://base:1234/v1"));
    assert_eq!(target.api_base(), Some("http://base:1234/v1"));
}

#[test]
fn profile_resolves_plaintext_api_key_and_env_default() {
    let mut source = MapSource::new()
        .with(
            ai_keys::TEXT_GENERATE_API_BASE,
            "${GCORE_TEST_UNSET_HOST:-http://default-host:1234/v1}",
        )
        .with(
            ai_keys::text_generate_profile_key("feature_low", ai_keys::PROFILE_API_KEY),
            "sk-plaintext-123",
        );

    let target = resolve_direct_generation_target(&mut source, "feature_low");
    // ${...:-default} expands without any env var set.
    assert_eq!(
        target.api_base.as_deref(),
        Some("http://default-host:1234/v1")
    );
    // Plaintext api_key is accepted in standalone YAML.
    assert_eq!(target.api_key.as_deref(), Some("sk-plaintext-123"));
}

#[test]
fn profile_resolves_api_keys_from_recognized_provider_environment() {
    let env = ProviderApiKeyEnvGuard::new();
    let cases = [
        ("anthropic", "ANTHROPIC_API_KEY", "anthropic-key"),
        ("openai", "OPENAI_API_KEY", "openai-key"),
        ("openrouter", "OPENROUTER_API_KEY", "openrouter-key"),
        ("groq", "GROQ_API_KEY", "groq-key"),
    ];

    for (provider, env_name, expected) in cases {
        env.set(env_name, &format!("  {expected}  "));
        let mut source = MapSource::new()
            .with(ai_keys::TEXT_GENERATE_PROVIDER, provider)
            .with(ai_keys::TEXT_GENERATE_API_BASE, "https://api.example/v1");

        let target = resolve_direct_generation_target(&mut source, "feature_low");

        assert_eq!(target.api_key.as_deref(), Some(expected), "{provider}");
    }

    env.set("OPENAI_API_KEY", "   ");
    let mut blank_env_source = MapSource::new().with(ai_keys::TEXT_GENERATE_PROVIDER, "openai");
    assert_eq!(
        resolve_direct_generation_target(&mut blank_env_source, "feature_low").api_key,
        None
    );

    env.set("OPENAI_API_KEY", "env-key");
    let mut configured_source = MapSource::new()
        .with(ai_keys::TEXT_GENERATE_PROVIDER, "openai")
        .with(ai_keys::TEXT_GENERATE_API_KEY, "configured-key");
    assert_eq!(
        resolve_direct_generation_target(&mut configured_source, "feature_low")
            .api_key
            .as_deref(),
        Some("configured-key")
    );
}

#[test]
fn profile_never_sends_openai_env_key_to_custom_or_unspecified_provider() {
    let env = ProviderApiKeyEnvGuard::new();
    env.set("OPENAI_API_KEY", "must-not-leak");

    for provider in [Some("custom"), None] {
        let mut source =
            MapSource::new().with(ai_keys::TEXT_GENERATE_API_BASE, "http://localhost:1234/v1");
        if let Some(provider) = provider {
            source = source.with(ai_keys::TEXT_GENERATE_PROVIDER, provider);
        }

        let target = resolve_direct_generation_target(&mut source, "feature_low");

        assert_eq!(target.api_key, None, "provider={provider:?}");
    }
}

#[test]
fn profile_unresolved_env_without_default_is_none() {
    let mut source = MapSource::new().with(
        ai_keys::TEXT_GENERATE_MODEL,
        "${GCORE_TEST_UNSET_NO_DEFAULT}",
    );
    let target = resolve_direct_generation_target(&mut source, "feature_mid");
    assert!(target.model.is_none());
    assert!(target.api_base.is_none());
}

// ----- Lane B: stub transport + trivial executor harness ---------------------
