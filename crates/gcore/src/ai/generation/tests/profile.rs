use super::common::*;

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
