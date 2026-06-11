//! Shared AI context, layered AI config source, and config-only routing.
//!
//! This module intentionally stays transport-free. It resolves the desired AI
//! bindings and routing from caller-provided config layers, then leaves any
//! probe-backed routing collapse to feature-gated transport code.

use std::sync::{Arc, Condvar, Mutex};

use crate::config::{
    AiCapability, AiRouting, AiTuning, CapabilityBinding, ConfigSource, resolve_ai_tuning,
    resolve_capability_binding,
};
use crate::provisioning::{StandaloneConfig, gcore_config_path};

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

        Self {
            bindings,
            tuning,
            limiter,
            project_id,
        }
    }

    pub fn binding(&self, capability: AiCapability) -> &CapabilityBinding {
        self.bindings.get(capability)
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
            AiCapability::TextGenerate => &self.text_generate,
        }
    }

    fn get_mut(&mut self, capability: AiCapability) -> &mut CapabilityBinding {
        match capability {
            AiCapability::Embed => &mut self.embed,
            AiCapability::AudioTranscribe => &mut self.audio_transcribe,
            AiCapability::AudioTranslate => &mut self.audio_translate,
            AiCapability::VisionExtract => &mut self.vision_extract,
            AiCapability::TextGenerate => &mut self.text_generate,
        }
    }

    fn force_routing(&mut self, routing: AiRouting) {
        for capability in ALL_CAPABILITIES {
            self.get_mut(capability).routing = routing;
        }
    }
}

#[cfg(test)]
const LOCAL_BACKEND_CAPABILITIES: [AiCapability; 3] = [
    AiCapability::Embed,
    AiCapability::VisionExtract,
    AiCapability::TextGenerate,
];

#[cfg(test)]
pub(crate) fn apply_discovered_local_backend(
    bindings: &mut AiBindings,
    backend: &crate::local_backend::Backend,
) {
    let api_base = crate::local_backend::backend_api_base(backend);
    for capability in LOCAL_BACKEND_CAPABILITIES {
        let binding = bindings.get_mut(capability);
        if binding_needs_local_api_base(binding) {
            binding.api_base = Some(api_base.clone());
        }
    }
}

#[cfg(test)]
fn binding_needs_local_api_base(binding: &CapabilityBinding) -> bool {
    matches!(binding.routing, AiRouting::Auto | AiRouting::Direct)
        && binding
            .api_base
            .as_deref()
            .map(str::trim)
            .is_none_or(str::is_empty)
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

/// Layered AI config source: primary config_store-like source, then gcore.yaml.
///
/// The source is intentionally AI-only: plain values are returned literally and
/// `$secret:` values are delegated to the primary source when present.
#[derive(Debug, Clone)]
pub struct AiConfigSource<P = NoPrimaryAiConfigSource> {
    primary: Option<P>,
    standalone: Option<StandaloneConfig>,
}

pub type LocalAiConfigSource = AiConfigSource<NoPrimaryAiConfigSource>;

impl LocalAiConfigSource {
    pub fn from_gobby_home(gobby_home: &std::path::Path) -> anyhow::Result<Self> {
        Ok(Self::with_primary(
            NoPrimaryAiConfigSource,
            StandaloneConfig::read_at(&gcore_config_path(gobby_home))?,
        ))
    }
}

impl<P> AiConfigSource<P>
where
    P: ConfigSource,
{
    pub fn with_primary(primary: P, standalone: Option<StandaloneConfig>) -> Self {
        Self {
            primary: Some(primary),
            standalone,
        }
    }

    pub fn with_primary_from_gobby_home(
        primary: P,
        gobby_home: &std::path::Path,
    ) -> anyhow::Result<Self> {
        Ok(Self::with_primary(
            primary,
            StandaloneConfig::read_at(&gcore_config_path(gobby_home))?,
        ))
    }
}

impl<P> ConfigSource for AiConfigSource<P>
where
    P: ConfigSource,
{
    fn config_value(&mut self, key: &str) -> Option<String> {
        self.primary
            .as_mut()
            .and_then(|source| source.config_value(key))
            .or_else(|| {
                self.standalone
                    .as_mut()
                    .and_then(|standalone| standalone.config_value(key))
            })
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.trim().starts_with("$secret:") {
            let Some(primary) = self.primary.as_mut() else {
                anyhow::bail!("secret resolution requires a daemon-backed AI config source");
            };
            return primary.resolve_value(value);
        }
        match self.standalone.as_mut() {
            Some(standalone) => standalone.resolve_value(value),
            None => Ok(value.to_string()),
        }
    }
}

/// Empty primary layer for local-only `gcore.yaml` resolution.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct NoPrimaryAiConfigSource;

impl ConfigSource for NoPrimaryAiConfigSource {
    fn config_value(&mut self, _key: &str) -> Option<String> {
        None
    }

    fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
        if value.trim().starts_with("$secret:") {
            anyhow::bail!("secret resolution requires a daemon-backed AI config source");
        }
        Ok(value.to_string())
    }
}

/// PostgreSQL config_store source for AI config.
#[cfg(feature = "postgres")]
pub struct PostgresAiConfigSource<'a, R> {
    conn: &'a mut postgres::Client,
    resolver: R,
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
            resolver,
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
        if value.trim().starts_with("$secret:") {
            return (self.resolver)(value, self.conn);
        }
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
    use crate::provisioning::gcore_config_path;
    use std::collections::HashMap;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::{Mutex, MutexGuard};

    static CWD_LOCK: Mutex<()> = Mutex::new(());

    struct TestSource {
        values: HashMap<&'static str, String>,
        resolved: HashMap<&'static str, String>,
    }

    impl TestSource {
        fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values
                    .into_iter()
                    .map(|(key, value)| (key, value.to_string()))
                    .collect(),
                resolved: HashMap::new(),
            }
        }

        fn with_resolved(
            mut self,
            values: impl IntoIterator<Item = (&'static str, &'static str)>,
        ) -> Self {
            self.resolved = values
                .into_iter()
                .map(|(key, value)| (key, value.to_string()))
                .collect();
            self
        }
    }

    impl ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).cloned()
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            self.resolved
                .get(value)
                .cloned()
                .ok_or_else(|| anyhow::anyhow!("unresolved test value: {value}"))
        }
    }

    struct CurrentDirGuard {
        _lock: MutexGuard<'static, ()>,
        original: PathBuf,
    }

    impl CurrentDirGuard {
        fn set(path: &std::path::Path) -> Self {
            let guard = CWD_LOCK
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let original = std::env::current_dir().expect("current dir");
            std::env::set_current_dir(path).expect("set current dir");
            Self {
                _lock: guard,
                original,
            }
        }
    }

    impl Drop for CurrentDirGuard {
        fn drop(&mut self) {
            std::env::set_current_dir(&self.original).expect("restore current dir");
        }
    }

    fn write_gcore_yaml(home: &std::path::Path, contents: &str) {
        let path = gcore_config_path(home);
        fs::create_dir_all(path.parent().expect("gcore config parent")).unwrap();
        fs::write(path, contents).unwrap();
    }

    fn binding(routing: AiRouting, api_base: Option<&str>) -> CapabilityBinding {
        CapabilityBinding {
            routing,
            transport: None,
            api_base: api_base.map(str::to_string),
            api_key: None,
            model: None,
            provider: None,
            task: None,
            language: None,
            target_lang: None,
            profile: None,
        }
    }

    #[test]
    fn resolves_in_db_and_no_db_modes() {
        let home = tempfile::tempdir().unwrap();
        write_gcore_yaml(
            home.path(),
            r#"
ai:
  embeddings:
    api_base: http://yaml-embedding
    model: yaml-embedding-model
    api_key: yaml-key
  audio_transcribe:
    routing: direct
  max_concurrency: 3
"#,
        );

        let mut no_db = LocalAiConfigSource::from_gobby_home(home.path()).unwrap();
        let no_db_context = AiContext::resolve(Some("yaml-project".to_string()), &mut no_db);

        let no_db_embed = no_db_context.binding(AiCapability::Embed);
        assert_eq!(
            no_db_embed.api_base.as_deref(),
            Some("http://yaml-embedding")
        );
        assert_eq!(no_db_embed.model.as_deref(), Some("yaml-embedding-model"));
        assert_eq!(no_db_embed.api_key.as_deref(), Some("yaml-key"));
        assert_eq!(
            route(&no_db_context, AiCapability::AudioTranscribe),
            AiRouting::Direct
        );
        assert_eq!(no_db_context.tuning.max_concurrency, 3);
        assert_eq!(no_db_context.limiter.max_concurrency(), 3);
        assert_eq!(no_db_context.project_id.as_deref(), Some("yaml-project"));

        let primary = TestSource::with_values([
            (ai_keys::EMBEDDINGS_API_BASE, "http://db-embedding"),
            (ai_keys::EMBEDDINGS_API_KEY, "$secret:db-embedding-key"),
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::MAX_CONCURRENCY, "2"),
        ])
        .with_resolved([("$secret:db-embedding-key", "resolved-db-key")]);
        let mut db = AiConfigSource::with_primary_from_gobby_home(primary, home.path()).unwrap();
        let db_context = AiContext::resolve(Some("db-project".to_string()), &mut db);

        let db_embed = db_context.binding(AiCapability::Embed);
        assert_eq!(db_embed.api_base.as_deref(), Some("http://db-embedding"));
        assert_eq!(db_embed.model.as_deref(), Some("yaml-embedding-model"));
        assert_eq!(db_embed.api_key.as_deref(), Some("resolved-db-key"));
        assert_eq!(
            route(&db_context, AiCapability::AudioTranscribe),
            AiRouting::Daemon
        );
        assert_eq!(db_context.tuning.max_concurrency, 2);
    }

    #[test]
    fn project_id_is_caller_supplied() {
        let home = tempfile::tempdir().unwrap();
        write_gcore_yaml(home.path(), "ai:\n  routing: direct\n");
        let cwd = tempfile::tempdir().unwrap();
        fs::create_dir_all(cwd.path().join(".gobby")).unwrap();
        fs::write(
            cwd.path().join(".gobby/project.json"),
            r#"{"id":"stray-cwd-project"}"#,
        )
        .unwrap();
        let _cwd = CurrentDirGuard::set(cwd.path());

        let mut topic_source = LocalAiConfigSource::from_gobby_home(home.path()).unwrap();
        let topic_context = AiContext::resolve(None, &mut topic_source);
        assert_eq!(topic_context.project_id, None);

        let mut project_source = LocalAiConfigSource::from_gobby_home(home.path()).unwrap();
        let project_context =
            AiContext::resolve(Some("scope-project".to_string()), &mut project_source);
        assert_eq!(project_context.project_id.as_deref(), Some("scope-project"));
    }

    #[test]
    fn db_without_config_store_falls_through() {
        let home = tempfile::tempdir().unwrap();
        write_gcore_yaml(
            home.path(),
            r#"
ai:
  text_generate:
    routing: direct
    api_base: http://yaml-text
"#,
        );
        let primary = TestSource::with_values([]);
        let mut source =
            AiConfigSource::with_primary_from_gobby_home(primary, home.path()).unwrap();

        let context = AiContext::resolve(None, &mut source);

        assert_eq!(
            route(&context, AiCapability::TextGenerate),
            AiRouting::Direct
        );
        assert_eq!(
            context
                .binding(AiCapability::TextGenerate)
                .api_base
                .as_deref(),
            Some("http://yaml-text")
        );
    }

    #[test]
    fn standalone_values_expand_env_patterns_for_db_fallback() {
        let home = tempfile::tempdir().unwrap();
        write_gcore_yaml(
            home.path(),
            r#"
ai:
  text_generate:
    routing: direct
    api_base: ${GOBBY_CONTEXT_TEST_MISSING:-http://expanded-text}
"#,
        );
        let primary = TestSource::with_values([]);
        let mut source =
            AiConfigSource::with_primary_from_gobby_home(primary, home.path()).unwrap();

        let context = AiContext::resolve(None, &mut source);

        assert_eq!(
            context
                .binding(AiCapability::TextGenerate)
                .api_base
                .as_deref(),
            Some("http://expanded-text")
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
        let mut source = AiConfigSource::with_primary(source, None);
        let context = AiContext::resolve(None, &mut source);
        assert_eq!(
            route(&context, AiCapability::AudioTranscribe),
            AiRouting::Daemon
        );
        assert_eq!(
            route(&context, AiCapability::VisionExtract),
            AiRouting::Direct
        );
        assert_eq!(route(&context, AiCapability::Embed), AiRouting::Auto);

        let source = TestSource::with_values([
            (ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon"),
            (ai_keys::VISION_EXTRACT_ROUTING, "off"),
        ]);
        let mut source = AiConfigSource::with_primary(source, None);
        let forced = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                forced_routing: Some(AiRouting::Direct),
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
            assert_eq!(route(&forced, capability), AiRouting::Direct);
        }

        let source = TestSource::with_values([(ai_keys::AUDIO_TRANSCRIBE_ROUTING, "daemon")]);
        let mut source = AiConfigSource::with_primary(source, None);
        let disabled = AiContext::resolve_with_options(
            None,
            &mut source,
            AiContextOptions {
                no_ai: true,
                forced_routing: Some(AiRouting::Direct),
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
        let mut source = AiConfigSource::with_primary(source, None);

        let context = AiContext::resolve(None, &mut source);

        assert_eq!(route(&context, AiCapability::Embed), AiRouting::Auto);
        assert_eq!(
            route(&context, AiCapability::VisionExtract),
            AiRouting::Direct
        );
        assert_eq!(
            route(&context, AiCapability::TextGenerate),
            AiRouting::Direct
        );
        assert_eq!(context.binding(AiCapability::Embed).api_base, None);
        assert_eq!(context.binding(AiCapability::VisionExtract).api_base, None);
        assert_eq!(context.binding(AiCapability::TextGenerate).api_base, None);
    }

    #[test]
    fn stt_not_autodiscovered_to_chat_backend() {
        let mut bindings = AiBindings {
            embed: binding(AiRouting::Auto, None),
            audio_transcribe: binding(AiRouting::Auto, None),
            audio_translate: binding(AiRouting::Direct, None),
            vision_extract: binding(AiRouting::Direct, None),
            text_generate: binding(AiRouting::Auto, None),
        };
        let backend = crate::local_backend::Backend {
            name: "lmstudio".into(),
            url: "http://localhost:1234".into(),
            probe: "/v1/models".into(),
            auth_token: "lmstudio".into(),
        };

        apply_discovered_local_backend(&mut bindings, &backend);

        assert_eq!(
            bindings.embed.api_base.as_deref(),
            Some("http://localhost:1234/v1")
        );
        assert_eq!(
            bindings.vision_extract.api_base.as_deref(),
            Some("http://localhost:1234/v1")
        );
        assert_eq!(
            bindings.text_generate.api_base.as_deref(),
            Some("http://localhost:1234/v1")
        );
        assert_eq!(bindings.audio_transcribe.api_base, None);
        assert_eq!(bindings.audio_translate.api_base, None);
    }
}
