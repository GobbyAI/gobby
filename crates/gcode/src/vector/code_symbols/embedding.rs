use std::collections::{HashMap, hash_map::Entry};
use std::sync::{Mutex, OnceLock};

use crate::config::{Context, EmbeddingConfig};
#[cfg(feature = "ai")]
use crate::db;
use crate::models::Symbol;
#[cfg(feature = "ai")]
use gobby_core::ai::{
    daemon,
    effective_config::{ai_source_for_conn, ai_source_without_primary},
    effective_route,
};
use gobby_core::ai_context::AiContext;
#[cfg(feature = "ai")]
use gobby_core::ai_types::AiError;
#[cfg(feature = "ai")]
use gobby_core::config::AiCapability;

use super::types::VectorLifecycleError;

const DIMENSION_PROBE_TEXT: &str = "dimension_probe";
static EMBEDDING_CLIENTS: OnceLock<Mutex<HashMap<u64, reqwest::blocking::Client>>> =
    OnceLock::new();

pub(super) fn dimension_probe_text() -> &'static str {
    DIMENSION_PROBE_TEXT
}

#[derive(Debug, Clone)]
pub enum EmbeddingSource {
    Daemon(Box<AiContext>),
    Direct(EmbeddingConfig),
}

impl From<EmbeddingConfig> for EmbeddingSource {
    fn from(config: EmbeddingConfig) -> Self {
        Self::Direct(config)
    }
}

impl From<AiContext> for EmbeddingSource {
    fn from(context: AiContext) -> Self {
        Self::Daemon(Box::new(context))
    }
}

#[derive(Debug, Clone)]
pub struct EmbeddingBackend {
    source: EmbeddingSource,
    direct_client: Option<reqwest::blocking::Client>,
}

impl EmbeddingBackend {
    pub fn new(source: EmbeddingSource) -> Result<Self, VectorLifecycleError> {
        let direct_client = match &source {
            EmbeddingSource::Direct(config) => {
                if config.api_base.trim().is_empty() {
                    return Err(VectorLifecycleError::MissingEmbeddingConfig);
                }
                Some(embedding_client(config)?)
            }
            EmbeddingSource::Daemon(_) => None,
        };
        Ok(Self {
            source,
            direct_client,
        })
    }

    pub fn embed_text(&self, text: &str) -> Result<Vec<f32>, VectorLifecycleError> {
        let texts = vec![text.to_string()];
        let mut embeddings = self.embed_text_batch(&texts)?;
        embeddings.pop().ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("embedding response was empty".to_string())
        })
    }

    pub fn embed_query(&self, text: &str) -> Result<Vec<f32>, VectorLifecycleError> {
        match &self.source {
            EmbeddingSource::Direct(config) => {
                let prefix = config.query_prefix.as_deref().unwrap_or("").trim();
                let input = if prefix.is_empty() {
                    text.to_string()
                } else {
                    format!("{prefix} {text}")
                };
                let client = self.direct_client.as_ref().ok_or_else(|| {
                    VectorLifecycleError::EmbeddingResponse(
                        "direct embedding client is not initialized".to_string(),
                    )
                })?;
                embed_text(client, config, &input)
            }
            EmbeddingSource::Daemon(context) => {
                embed_via_daemon_or_err(context, &[text.to_string()]).and_then(|embeddings| {
                    embeddings.into_iter().next().ok_or_else(|| {
                        VectorLifecycleError::EmbeddingResponse(
                            "daemon embedding response was empty".to_string(),
                        )
                    })
                })
            }
        }
    }

    pub fn embed_text_batch(
        &self,
        texts: &[String],
    ) -> Result<Vec<Vec<f32>>, VectorLifecycleError> {
        match &self.source {
            EmbeddingSource::Direct(config) => {
                let client = self.direct_client.as_ref().ok_or_else(|| {
                    VectorLifecycleError::EmbeddingResponse(
                        "direct embedding client is not initialized".to_string(),
                    )
                })?;
                embed_text_batch(client, config, texts)
            }
            EmbeddingSource::Daemon(context) => embed_via_daemon_or_err(context, texts),
        }
    }
}

#[cfg(feature = "ai")]
const INDEXING_EMBED_QUERY_MODE: bool = false;

fn embed_via_daemon_or_err(
    context: &AiContext,
    texts: &[String],
) -> Result<Vec<Vec<f32>>, VectorLifecycleError> {
    #[cfg(feature = "ai")]
    {
        daemon::embed_via_daemon(context, texts, INDEXING_EMBED_QUERY_MODE)
            .map(|result| result.embeddings)
            .map_err(|error| VectorLifecycleError::EmbeddingResponse(error.to_string()))
    }
    #[cfg(not(feature = "ai"))]
    {
        let _ = (context, texts);
        Err(VectorLifecycleError::EmbeddingResponse(
            "gcode built without the ai feature".to_string(),
        ))
    }
}

pub fn embedding_source_from_context(ctx: &Context) -> Option<EmbeddingSource> {
    #[cfg(feature = "ai")]
    {
        let resolved = resolve_embedding_ai_context(ctx)?;
        embedding_source_from_resolved_ai_context(resolved.context, resolved.direct_config)
    }
    #[cfg(not(feature = "ai"))]
    {
        let _ = ctx;
        None
    }
}

#[cfg(feature = "ai")]
fn embedding_source_from_resolved_ai_context(
    ai_context: AiContext,
    direct_config: Option<EmbeddingConfig>,
) -> Option<EmbeddingSource> {
    let _ = direct_config;
    match effective_route(&ai_context, AiCapability::Embed) {
        gobby_core::config::AiRouting::Off => None,
        gobby_core::config::AiRouting::Daemon => {
            Some(EmbeddingSource::Daemon(Box::new(ai_context)))
        }
    }
}

#[cfg(feature = "ai")]
struct ResolvedEmbeddingAiContext {
    context: AiContext,
    direct_config: Option<EmbeddingConfig>,
}

#[cfg(feature = "ai")]
fn resolve_embedding_ai_context(ctx: &Context) -> Option<ResolvedEmbeddingAiContext> {
    if let Ok(mut conn) = db::connect_readonly(&ctx.database_url) {
        let mut source = effective_ai_source(ai_source_for_conn(&mut conn))?;
        let mut context = AiContext::resolve(Some(ctx.project_id.clone()), &mut source);
        attach_grant(&mut context, ctx);
        let direct_config = gobby_core::config::resolve_embedding_config_from_binding(
            &mut source,
            context.binding(AiCapability::Embed),
        );
        return Some(ResolvedEmbeddingAiContext {
            context,
            direct_config,
        });
    }

    let mut source = effective_ai_source(ai_source_without_primary())?;
    let mut context = AiContext::resolve(Some(ctx.project_id.clone()), &mut source);
    attach_grant(&mut context, ctx);
    if let Some(embedding) = &ctx.embedding {
        context.bindings.embed.api_base = Some(embedding.api_base.clone());
        context.bindings.embed.model = Some(embedding.model.clone());
        context.bindings.embed.api_key = embedding.api_key.clone();
    }
    let direct_config = gobby_core::config::resolve_embedding_config_from_binding(
        &mut source,
        context.binding(AiCapability::Embed),
    )
    .or_else(|| ctx.embedding.clone());
    Some(ResolvedEmbeddingAiContext {
        context,
        direct_config,
    })
}

#[cfg(feature = "ai")]
fn attach_grant(context: &mut AiContext, ctx: &Context) {
    if let Some(grant) = &ctx.grant_ai {
        context.grant = Some(gobby_core::ai_context::GrantAiState {
            capabilities: grant.capabilities.clone(),
            daemon_reachable: grant.daemon_reachable,
            bundle: grant.bundle.clone(),
        });
    }
}

#[cfg(feature = "ai")]
fn effective_ai_source<T>(source: anyhow::Result<T>) -> Option<T> {
    match source {
        Ok(source) => Some(source),
        Err(error) => {
            log::warn!("failed to resolve effective AI config: {error}");
            None
        }
    }
}

pub fn embedding_client(
    config: &EmbeddingConfig,
) -> Result<reqwest::blocking::Client, VectorLifecycleError> {
    let mut clients = match EMBEDDING_CLIENTS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
    {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    };
    // The blocking HTTP client is keyed only by timeout because request-specific
    // embedding endpoint, model, and auth details are applied per request.
    match clients.entry(config.timeout_seconds) {
        Entry::Occupied(entry) => Ok(entry.get().clone()),
        Entry::Vacant(entry) => {
            let client = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(config.timeout_seconds))
                .build()
                .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
            Ok(entry.insert(client).clone())
        }
    }
}

pub fn embed_text(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    text: &str,
) -> Result<Vec<f32>, VectorLifecycleError> {
    #[cfg(feature = "ai")]
    {
        gobby_core::ai::embeddings::embed_one(client, config, text).map_err(embedding_error)
    }
    #[cfg(not(feature = "ai"))]
    {
        let _ = (client, config, text);
        Err(VectorLifecycleError::EmbeddingResponse(
            "gcode built without the ai feature".to_string(),
        ))
    }
}

pub fn probe_embedding_dim(config: &EmbeddingConfig) -> Result<usize, VectorLifecycleError> {
    let client = embedding_client(config)?;
    Ok(embed_text(&client, config, dimension_probe_text())?.len())
}

pub fn embed_text_batch(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    texts: &[String],
) -> Result<Vec<Vec<f32>>, VectorLifecycleError> {
    #[cfg(feature = "ai")]
    {
        gobby_core::ai::embeddings::embed_batch(client, config, texts).map_err(embedding_error)
    }
    #[cfg(not(feature = "ai"))]
    {
        let _ = (client, config, texts);
        Err(VectorLifecycleError::EmbeddingResponse(
            "gcode built without the ai feature".to_string(),
        ))
    }
}

#[cfg(feature = "ai")]
fn embedding_error(error: AiError) -> VectorLifecycleError {
    match error {
        AiError::HttpStatus { status, body } => VectorLifecycleError::EmbeddingHttp {
            status,
            body: body.unwrap_or_default(),
        },
        AiError::RateLimited {
            status: Some(status),
            body,
            ..
        } => VectorLifecycleError::EmbeddingHttp {
            status,
            body: body.unwrap_or_default(),
        },
        AiError::TransportFailure {
            status: Some(status),
            body: Some(body),
            ..
        } => VectorLifecycleError::EmbeddingHttp { status, body },
        other => VectorLifecycleError::EmbeddingResponse(other.to_string()),
    }
}

pub fn embed_query_with_source(source: &EmbeddingSource, text: &str) -> Option<Vec<f32>> {
    let backend = match EmbeddingBackend::new(source.clone()) {
        Ok(backend) => backend,
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            return None;
        }
    };
    match backend.embed_query(text) {
        Ok(embedding) => Some(embedding),
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            None
        }
    }
}

pub fn vector_text_for_symbol(symbol: &Symbol) -> String {
    let mut lines = vec![
        format!("name: {}", symbol.name),
        format!("qualified_name: {}", symbol.qualified_name),
        format!("kind: {}", symbol.kind),
        format!("language: {}", symbol.language),
        format!("file_path: {}", symbol.file_path),
        format!("range: {}-{}", symbol.line_start, symbol.line_end),
    ];
    if let Some(signature) = symbol
        .signature
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("signature: {signature}"));
    }
    if let Some(docstring) = symbol
        .docstring
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("docstring: {docstring}"));
    }
    if let Some(summary) = symbol
        .summary
        .as_deref()
        .filter(|value| !value.trim().is_empty())
    {
        lines.push(format!("summary: {summary}"));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    #[cfg(feature = "ai")]
    use super::EmbeddingSource;
    #[cfg(feature = "ai")]
    use super::embedding_source_from_resolved_ai_context;
    #[cfg(feature = "ai")]
    use gobby_core::ai_context::AiContext;
    use gobby_core::config::{ConfigSource, ai_keys};
    use std::collections::HashMap;

    #[derive(Default)]
    struct TestSource {
        values: HashMap<&'static str, &'static str>,
    }

    impl TestSource {
        fn with_values(values: impl IntoIterator<Item = (&'static str, &'static str)>) -> Self {
            Self {
                values: values.into_iter().collect(),
            }
        }
    }

    impl ConfigSource for TestSource {
        fn config_value(&mut self, key: &str) -> Option<String> {
            self.values.get(key).map(|value| (*value).to_string())
        }

        fn resolve_value(&mut self, value: &str) -> anyhow::Result<String> {
            match value {
                "secret-marker EMBEDDING_KEY" => Ok("resolved-embedding-key".to_string()),
                value => Ok(value.to_string()),
            }
        }
    }

    #[test]
    fn resolves_via_shared_routing() {
        let mut daemon_source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "daemon"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://daemon-should-not-be-used:11434/v1",
            ),
        ]);
        let daemon = crate::config::resolve_embedding_config_from_source(None, &mut daemon_source)
            .expect("daemon-served embedding config");
        assert_eq!(daemon.api_base, "http://daemon-should-not-be-used:11434/v1");

        let mut off_source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "off"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://off-should-not-be-used:11434/v1",
            ),
        ]);
        assert!(
            crate::config::resolve_embedding_config_from_source(None, &mut off_source).is_none()
        );
    }

    #[test]
    fn reads_endpoint_from_shared_binding() {
        let mut source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "daemon"),
            (ai_keys::EMBEDDINGS_TRANSPORT, "openai_compatible_http"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://shared-binding.local:11434/v1",
            ),
            (ai_keys::EMBEDDINGS_MODEL, "shared-embed-model"),
            (ai_keys::EMBEDDINGS_API_KEY, "secret-marker EMBEDDING_KEY"),
        ]);

        let config = crate::config::resolve_embedding_config_from_source(None, &mut source)
            .expect("daemon-served embedding config");
        assert_eq!(config.api_base, "http://shared-binding.local:11434/v1");
        assert_eq!(config.model, "shared-embed-model");
        assert_eq!(config.api_key.as_deref(), Some("resolved-embedding-key"));
    }

    #[test]
    #[cfg(feature = "ai")]
    fn daemon_source_is_selected_for_daemon_route() {
        let mut source = TestSource::with_values([(ai_keys::EMBEDDINGS_ROUTING, "daemon")]);
        let context = AiContext::resolve(None, &mut source);

        let source = embedding_source_from_resolved_ai_context(context, None);

        match source {
            Some(EmbeddingSource::Daemon(_)) => {}
            other => panic!("expected daemon embedding source, got {other:?}"),
        }
    }

    #[test]
    #[cfg(feature = "ai")]
    fn embed_via_daemon_or_err_uses_document_mode_for_indexing() {
        const {
            assert!(
                !super::INDEXING_EMBED_QUERY_MODE,
                "indexing embeddings must use document mode"
            );
        }
    }
}
