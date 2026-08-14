//! Shared AI context, layered AI config source, and config-only routing.
//!
//! This module intentionally stays transport-free. It resolves the desired AI
//! bindings and routing from caller-provided config layers, then leaves any
//! probe-backed routing collapse to feature-gated transport code.

use std::sync::{Arc, Condvar, Mutex};

#[cfg(feature = "ai")]
use crate::ai::generation::ToolLoopLimits;
use crate::config::{
    AiCapability, AiRouting, AiTuning, CapabilityBinding, ConfigSource, reject_secret_marker,
    resolve_ai_tuning, resolve_capability_binding,
};

const ALL_CAPABILITIES: [AiCapability; 5] = [
    AiCapability::Embed,
    AiCapability::AudioTranscribe,
    AiCapability::AudioTranslate,
    AiCapability::VisionExtract,
    AiCapability::TextGenerate,
];

/// Resolved AI context shared by gcore consumers.
#[derive(Debug, Clone)]
pub struct AiContext {
    pub bindings: AiBindings,
    pub tuning: AiTuning,
    pub limiter: AiLimiter,
    pub project_id: Option<String>,
    pub grant: Option<GrantAiState>,
    #[cfg(feature = "ai")]
    pub tool_loop_limits: ToolLoopLimits,
}

/// Grant snapshot used to gate AI calls before HTTP.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GrantAiState {
    pub capabilities: crate::grant::GrantCapabilities,
    pub daemon_reachable: bool,
}

impl AiContext {
    /// Resolve AI context from a caller-supplied project authority and source.
    pub fn resolve(project_id: Option<String>, source: &mut impl ConfigSource) -> Self {
        Self::resolve_with_options(project_id, source, AiContextOptions::default())
    }

    /// Resolve AI context with command-scoped routing overrides.
    pub fn resolve_with_options(
        project_id: Option<String>,
        source: &mut impl ConfigSource,
        options: AiContextOptions,
    ) -> Self {
        Self::resolve_with_options_inner(project_id, source, options, false)
            .expect("non-strict AI context resolution cannot fail")
    }

    #[cfg(feature = "ai")]
    pub fn try_resolve_with_options(
        project_id: Option<String>,
        source: &mut impl ConfigSource,
        options: AiContextOptions,
    ) -> anyhow::Result<Self> {
        Self::resolve_with_options_inner(project_id, source, options, true)
    }

    fn resolve_with_options_inner(
        project_id: Option<String>,
        source: &mut impl ConfigSource,
        options: AiContextOptions,
        strict_tool_loop_limits: bool,
    ) -> anyhow::Result<Self> {
        let mut bindings = AiBindings::resolve(source);
        let mut tuning = resolve_ai_tuning(source);

        if options.no_ai {
            bindings.force_routing(AiRouting::Off);
        } else if let Some(routing) = options.forced_routing {
            bindings.force_routing(routing);
        }

        if tuning.max_concurrency == 0 {
            tuning.max_concurrency = 1;
        }
        let limiter = AiLimiter::new(tuning.max_concurrency);
        #[cfg(feature = "ai")]
        let tool_loop_limits = match ToolLoopLimits::resolve(source) {
            Ok(limits) => limits,
            Err(error) if !strict_tool_loop_limits => {
                log::warn!("failed to resolve tool-loop limits; using defaults: {error}");
                ToolLoopLimits::default()
            }
            Err(error) => return Err(error.into()),
        };
        #[cfg(not(feature = "ai"))]
        let _ = strict_tool_loop_limits;

        Ok(Self {
            bindings,
            tuning,
            limiter,
            project_id,
            grant: None,
            #[cfg(feature = "ai")]
            tool_loop_limits,
        })
    }

    pub fn binding(&self, capability: AiCapability) -> &CapabilityBinding {
        self.bindings.get(capability)
    }

    #[cfg(feature = "ai")]
    pub fn require_granted(
        &self,
        capability: AiCapability,
    ) -> Result<(), crate::ai_types::AiError> {
        let Some(grant) = &self.grant else {
            return Ok(());
        };
        crate::ai::require_modality_ready(&grant.capabilities, grant.daemon_reachable, capability)
    }
}

/// Command-scoped AI context overrides.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AiContextOptions {
    pub no_ai: bool,
    pub forced_routing: Option<AiRouting>,
}

/// Per-capability AI bindings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AiBindings {
    pub embed: CapabilityBinding,
    pub audio_transcribe: CapabilityBinding,
    pub audio_translate: CapabilityBinding,
    pub vision_extract: CapabilityBinding,
    pub text_generate: CapabilityBinding,
}

impl AiBindings {
    pub fn resolve(source: &mut impl ConfigSource) -> Self {
        Self {
            embed: resolve_capability_binding(source, AiCapability::Embed),
            audio_transcribe: resolve_capability_binding(source, AiCapability::AudioTranscribe),
            audio_translate: resolve_capability_binding(source, AiCapability::AudioTranslate),
            vision_extract: resolve_capability_binding(source, AiCapability::VisionExtract),
            text_generate: resolve_capability_binding(source, AiCapability::TextGenerate),
        }
    }

    pub fn get(&self, capability: AiCapability) -> &CapabilityBinding {
        match capability {
            AiCapability::Embed => &self.embed,
            AiCapability::AudioTranscribe => &self.audio_transcribe,
            AiCapability::AudioTranslate => &self.audio_translate,
            AiCapability::VisionExtract => &self.vision_extract,
            // ToolChat has no binding of its own: it routes off the
            // resolved text_generate binding, so tool-capability filtering
            // layers on the same provider/model/profile config.
            AiCapability::TextGenerate | AiCapability::ToolChat => &self.text_generate,
        }
    }

    fn get_mut(&mut self, capability: AiCapability) -> &mut CapabilityBinding {
        match capability {
            AiCapability::Embed => &mut self.embed,
            AiCapability::AudioTranscribe => &mut self.audio_transcribe,
            AiCapability::AudioTranslate => &mut self.audio_translate,
            AiCapability::VisionExtract => &mut self.vision_extract,
            AiCapability::TextGenerate | AiCapability::ToolChat => &mut self.text_generate,
        }
    }

    fn force_routing(&mut self, routing: AiRouting) {
        for capability in ALL_CAPABILITIES {
            self.get_mut(capability).routing = routing;
        }
    }
}

/// Return the config-only desired route for a capability.
pub fn route(context: &AiContext, capability: AiCapability) -> AiRouting {
    context.binding(capability).routing
}

/// Shared blocking concurrency limiter for AI transports.
#[derive(Clone)]
pub struct AiLimiter {
    inner: Arc<LimiterInner>,
}

struct LimiterInner {
    max: u8,
    active: Mutex<u8>,
    available: Condvar,
}

impl AiLimiter {
    pub fn new(max_concurrency: u8) -> Self {
        Self {
            inner: Arc::new(LimiterInner {
                max: max_concurrency.max(1),
                active: Mutex::new(0),
                available: Condvar::new(),
            }),
        }
    }

    pub fn max_concurrency(&self) -> u8 {
        self.inner.max
    }

    pub fn acquire(&self) -> AiPermit {
        let mut active = self
            .inner
            .active
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        while *active >= self.inner.max {
            active = self
                .inner
                .available
                .wait(active)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
        *active += 1;
        AiPermit {
            inner: Arc::clone(&self.inner),
        }
    }

    pub fn try_acquire(&self) -> Option<AiPermit> {
        let mut active = self
            .inner
            .active
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if *active >= self.inner.max {
            return None;
        }
        *active += 1;
        Some(AiPermit {
            inner: Arc::clone(&self.inner),
        })
    }
}

impl std::fmt::Debug for AiLimiter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AiLimiter")
            .field("max_concurrency", &self.max_concurrency())
            .finish_non_exhaustive()
    }
}

/// Permit returned by [`AiLimiter`].
#[derive(Debug)]
pub struct AiPermit {
    inner: Arc<LimiterInner>,
}

impl Drop for AiPermit {
    fn drop(&mut self) {
        let mut active = self
            .inner
            .active
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *active = active.saturating_sub(1);
        self.inner.available.notify_one();
    }
}

impl std::fmt::Debug for LimiterInner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LimiterInner")
            .field("max", &self.max)
            .finish_non_exhaustive()
    }
}

/// Grant-backed AI config source. Secret markers fail typed as grant-issuance bugs.
#[derive(Debug, Clone)]
pub struct AiConfigSource<P = NoPrimaryAiConfigSource> {
    primary: Option<P>,
}

pub type LocalAiConfigSource = AiConfigSource<NoPrimaryAiConfigSource>;

impl LocalAiConfigSource {
    pub fn empty() -> Self {
        Self { primary: None }
    }
}

impl<P> AiConfigSource<P>
where
    P: ConfigSource,
{
    pub fn with_primary(primary: P) -> Self {
        Self {
            primary: Some(primary),
        }
    }
}

impl<P> ConfigSource for AiConfigSource<P>
where
    P: ConfigSource,
{
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        match self.primary.as_mut() {
            Some(source) => source.snapshot_revision(),
            None => Ok(None),
        }
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        self.primary
            .as_mut()
            .and_then(|source| source.config_value(key))
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        reject_secret_marker(value)?;
        match self.primary.as_mut() {
            Some(primary) => primary.resolve_value(value),
            None => resolve_non_secret_config_value(value),
        }
    }
}

fn resolve_non_secret_config_value(value: &str) -> anyhow::Result<String> {
    crate::config::resolve_env_pattern(value)?
        .ok_or_else(|| anyhow::anyhow!("unresolved pattern: {value}"))
}

/// Empty primary layer for callers without a grant-backed AI source.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct NoPrimaryAiConfigSource;

impl ConfigSource for NoPrimaryAiConfigSource {
    fn config_value(&mut self, _key: &str) -> Option<String> {
        None
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        reject_secret_marker(value)?;
        resolve_non_secret_config_value(value)
    }
}

/// PostgreSQL config_store source for AI config.
#[cfg(feature = "postgres")]
pub struct PostgresAiConfigSource<'a, R> {
    conn: &'a mut postgres::Client,
    _resolver: R,
    config_store_available: bool,
}

#[cfg(feature = "postgres")]
impl<'a, R> PostgresAiConfigSource<'a, R>
where
    R: FnMut(&str, &mut postgres::Client) -> anyhow::Result<String>,
{
    pub fn new(conn: &'a mut postgres::Client, resolver: R) -> Self {
        Self {
            conn,
            _resolver: resolver,
            config_store_available: true,
        }
    }

    pub fn config_store_available(&self) -> bool {
        self.config_store_available
    }
}

#[cfg(feature = "postgres")]
impl<R> ConfigSource for PostgresAiConfigSource<'_, R>
where
    R: FnMut(&str, &mut postgres::Client) -> anyhow::Result<String>,
{
    fn snapshot_revision(&mut self) -> anyhow::Result<Option<i64>> {
        if !self.config_store_available {
            return Ok(None);
        }
        crate::postgres::read_config_revision(self.conn).map(Some)
    }

    fn config_value(&mut self, key: &str) -> Option<String> {
        if !self.config_store_available {
            return None;
        }
        match crate::postgres::read_config_value(self.conn, key) {
            Ok(raw) => raw.and_then(|raw| crate::config::decode_config_value(&raw)),
            Err(error) if config_store_missing(&error) => {
                self.config_store_available = false;
                None
            }
            Err(error) => {
                log::warn!("failed to read AI config key {key:?}: {error}");
                None
            }
        }
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        reject_secret_marker(value)?;
        Ok(value.to_string())
    }
}

#[cfg(feature = "postgres")]
fn config_store_missing(error: &anyhow::Error) -> bool {
    error.chain().any(|source| {
        source
            .downcast_ref::<postgres::Error>()
            .and_then(postgres::Error::as_db_error)
            .is_some_and(|db_error| *db_error.code() == postgres::error::SqlState::UNDEFINED_TABLE)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AiCapability, AiRouting, ConfigSource, ai_keys};
    use std::collections::HashMap;

    struct TestSource {
        values: HashMap<&'static str, String>,
    }

    impl TestSource {
        fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .map(|(key, value)| (key, value.to_string()))
                    .collect(),
            }
        }
    }

    impl ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            reject_secret_marker(value)?;
            Ok(value.to_string())
        }
    }

    #[test]
    fn resolves_from_grant_backed_primary() {
        let mut source = AiConfigSource::with_primary(TestSource::with_values([
            (ai_keys::EMBEDDINGS_API_BASE, "http://grant-embedding"),
            (ai_keys::EMBEDDINGS_MODEL, "grant-embedding-model"),
            (ai_keys::EMBEDDINGS_API_KEY, "grant-key"),
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::MAX_CONCURRENCY, "3"),
        ]));
        let context = AiContext::resolve(Some("grant-project".to_string()), &mut source);

        let embed = context.binding(AiCapability::Embed);
        assert_eq!(embed.api_base.as_deref(), Some("http://grant-embedding"));
        assert_eq!(embed.model.as_deref(), Some("grant-embedding-model"));
        assert_eq!(embed.api_key.as_deref(), Some("grant-key"));
        assert_eq!(
            route(&context, AiCapability::AudioTranscribe),
            AiRouting::Daemon
        );
        assert_eq!(context.tuning.max_concurrency, 3);
        assert_eq!(context.limiter.max_concurrency(), 3);
        assert_eq!(context.project_id.as_deref(), Some("grant-project"));
    }

    #[test]
    fn project_id_is_caller_supplied() {
        let mut topic_source = LocalAiConfigSource::empty();
        let topic_context = AiContext::resolve(None, &mut topic_source);
        assert_eq!(topic_context.project_id, None);

        let mut project_source = LocalAiConfigSource::empty();
        let project_context =
            AiContext::resolve(Some("scope-project".to_string()), &mut project_source);
        assert_eq!(project_context.project_id.as_deref(), Some("scope-project"));
    }

    #[test]
    fn missing_primary_uses_defaults() {
        let mut source = LocalAiConfigSource::empty();
        let context = AiContext::resolve(None, &mut source);
        assert_eq!(
            route(&context, AiCapability::TextGenerate),
            AiRouting::Daemon
        );
        assert_eq!(
            context
                .binding(AiCapability::TextGenerate)
                .api_base
                .as_deref(),
            None
        );
    }

    #[test]
    fn tool_chat_reuses_the_text_generate_binding() {
        let mut source = AiConfigSource::with_primary(TestSource::with_values([
            (ai_keys::TEXT_GENERATE_API_BASE, "http://grant-text"),
            (ai_keys::TEXT_GENERATE_REASONING_EFFORT, "high"),
        ]));

        let context = AiContext::resolve(None, &mut source);

        assert_eq!(route(&context, AiCapability::ToolChat), AiRouting::Daemon);
        assert_eq!(
            context.binding(AiCapability::ToolChat),
            context.binding(AiCapability::TextGenerate)
        );
        assert_eq!(
            context
                .binding(AiCapability::ToolChat)
                .reasoning_effort
                .as_deref(),
            Some("high")
        );
    }

    #[cfg(feature = "ai")]
    #[test]
    fn strict_tool_loop_context_rejects_invalid_configured_limits() {
        let mut direct_source =
            TestSource::with_values([("ai.generation.tool_loop.max_tool_calls", "0")])
                .with_resolved([("0", "0")]);
        ToolLoopLimits::resolve(&mut direct_source)
            .expect_err("the test source must expose the invalid limit");
        let mut context_source =
            TestSource::with_values([("ai.generation.tool_loop.max_tool_calls", "0")])
                .with_resolved([("0", "0")]);

        let error = AiContext::try_resolve_with_options(
            None,
            &mut context_source,
            AiContextOptions::default(),
        )
        .expect_err("zero tool-loop limits must be rejected");

        assert!(error.to_string().contains("max_tool_calls"));
    }

    #[test]
    fn secret_markers_fail_typed_on_ai_source() {
        let marker = crate::config::secret_marker_prefix() + "embedding_api_key";
        let mut source = LocalAiConfigSource::empty();
        let error = source
            .resolve_value(&marker)
            .expect_err("secret marker must fail typed");
        assert!(format!("{error:#}").contains("grant-issuance"));
    }

    #[test]
    fn no_primary_source_expands_env_patterns() {
        let mut source = NoPrimaryAiConfigSource;

        assert_eq!(
            source
                .resolve_value("${GOBBY_AI_CONTEXT_NO_PRIMARY_TEST_MISSING:-http://fallback}")
                .unwrap(),
            "http://fallback"
        );
    }

    #[test]
    fn concurrency_cap_enforced() {
        let limiter = AiLimiter::new(1);
        let permit = limiter
            .try_acquire()
            .expect("first permit should be available");

        assert!(limiter.try_acquire().is_none());

        drop(permit);

        assert!(limiter.try_acquire().is_some());
    }

    #[test]
    fn forced_routing_and_no_ai_override() {
        let source = TestSource::with_values([
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::VISION_EXTRACT_ROUTING, "direct"),
        ]);
        let mut source = AiConfigSource::with_primary(source);
        let context = AiContext::resolve(None, &mut source);
        assert_eq!(
            route(&context, AiCapability::AudioTranscribe),
            AiRouting::Daemon
        );
        assert_eq!(
            route(&context, AiCapability::VisionExtract),
            AiRouting::Daemon
        );
        assert_eq!(route(&context, AiCapability::Embed), AiRouting::Daemon);

        let source = TestSource::with_values([
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::VISION_EXTRACT_ROUTING, "off"),
        ]);
        let mut source = AiConfigSource::with_primary(source);
        let forced = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                forced_routing: Some(AiRouting::Daemon),
                ..AiContextOptions::default()
            },
        );
        for capability in [
            AiCapability::Embed,
            AiCapability::AudioTranscribe,
            AiCapability::AudioTranslate,
            AiCapability::VisionExtract,
            AiCapability::TextGenerate,
        ] {
            assert_eq!(route(&forced, capability), AiRouting::Daemon);
        }

        let source = TestSource::with_values([(ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon")]);
        let mut source = AiConfigSource::with_primary(source);
        let disabled = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: true,
                forced_routing: Some(AiRouting::Daemon),
            },
        );
        for capability in [
            AiCapability::Embed,
            AiCapability::AudioTranscribe,
            AiCapability::AudioTranslate,
            AiCapability::VisionExtract,
            AiCapability::TextGenerate,
        ] {
            assert_eq!(route(&disabled, capability), AiRouting::Off);
        }
    }

    #[test]
    fn resolve_does_not_discover_local_backend_endpoints() {
        let source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "auto"),
            (ai_keys::VISION_EXTRACT_ROUTING, "direct"),
            (ai_keys::TEXT_GENERATE_ROUTING, "direct"),
        ]);
        let mut source = AiConfigSource::with_primary(source);

        let context = AiContext::resolve(None, &mut source);

        assert_eq!(route(&context, AiCapability::Embed), AiRouting::Daemon);
        assert_eq!(
            route(&context, AiCapability::VisionExtract),
            AiRouting::Daemon
        );
        assert_eq!(
            route(&context, AiCapability::TextGenerate),
            AiRouting::Daemon
        );
        assert_eq!(context.binding(AiCapability::Embed).api_base, None);
        assert_eq!(context.binding(AiCapability::VisionExtract).api_base, None);
        assert_eq!(context.binding(AiCapability::TextGenerate).api_base, None);
    }
}
