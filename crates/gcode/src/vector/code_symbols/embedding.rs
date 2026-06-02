use serde_json::{Value, json};
use std::collections::{HashMap, hash_map::Entry};
use std::sync::{Mutex, OnceLock};

use crate::config::{Context, EmbeddingConfig};
use crate::db;
use crate::models::Symbol;
use crate::secrets;
use gobby_core::ai::{daemon, effective_route};
use gobby_core::ai_context::{
    AiConfigSource, AiContext, NoPrimaryAiConfigSource, PostgresAiConfigSource,
};
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
                let texts = vec![text.to_string()];
                let result = daemon::embed_via_daemon(context, &texts, true)
                    .map_err(|error| VectorLifecycleError::EmbeddingResponse(error.to_string()))?;
                result.embeddings.into_iter().next().ok_or_else(|| {
                    VectorLifecycleError::EmbeddingResponse(
                        "daemon embedding response was empty".to_string(),
                    )
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
            EmbeddingSource::Daemon(context) => daemon::embed_via_daemon(context, texts, false)
                .map(|result| result.embeddings)
                .map_err(|error| VectorLifecycleError::EmbeddingResponse(error.to_string())),
        }
    }
}

pub fn embedding_source_from_context(ctx: &Context) -> Option<EmbeddingSource> {
    let resolved = resolve_embedding_ai_context(ctx);
    embedding_source_from_resolved_ai_context(resolved.context, resolved.direct_config)
}

fn embedding_source_from_resolved_ai_context(
    ai_context: AiContext,
    direct_config: Option<EmbeddingConfig>,
) -> Option<EmbeddingSource> {
    match effective_route(&ai_context, AiCapability::Embed) {
        gobby_core::config::AiRouting::Off => None,
        gobby_core::config::AiRouting::Daemon => {
            Some(EmbeddingSource::Daemon(Box::new(ai_context)))
        }
        gobby_core::config::AiRouting::Direct => direct_config.map(EmbeddingSource::Direct),
        gobby_core::config::AiRouting::Auto => None,
    }
}

struct ResolvedEmbeddingAiContext {
    context: AiContext,
    direct_config: Option<EmbeddingConfig>,
}

fn resolve_embedding_ai_context(ctx: &Context) -> ResolvedEmbeddingAiContext {
    let standalone = crate::config::read_standalone_config_optional();
    if let Ok(mut conn) = db::connect_readonly(&ctx.database_url) {
        let primary = PostgresAiConfigSource::new(&mut conn, secrets::resolve_config_value);
        let mut source = AiConfigSource::with_primary(primary, standalone);
        let context = AiContext::resolve(Some(ctx.project_id.clone()), &mut source);
        let direct_config = gobby_core::config::resolve_embedding_config_from_binding(
            &mut source,
            context.binding(AiCapability::Embed),
        );
        return ResolvedEmbeddingAiContext {
            context,
            direct_config,
        };
    }

    let mut source = AiConfigSource::with_primary(NoPrimaryAiConfigSource, standalone);
    let mut context = AiContext::resolve(Some(ctx.project_id.clone()), &mut source);
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
    ResolvedEmbeddingAiContext {
        context,
        direct_config,
    }
}

pub fn embedding_client(
    config: &EmbeddingConfig,
) -> Result<reqwest::blocking::Client, VectorLifecycleError> {
    let mut clients = EMBEDDING_CLIENTS
        .get_or_init(|| Mutex::new(HashMap::new()))
        .lock()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
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
    let body = json!({
        "model": config.model,
        "input": text,
    });

    let data = send_embedding_request(client, config, body)?;
    data.get("data")
        .and_then(Value::as_array)
        .and_then(|values| values.first())
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing data[0] object".to_string())
        })
        .and_then(parse_embedding)
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
    if texts.is_empty() {
        return Ok(Vec::new());
    }
    let body = json!({
        "model": config.model,
        "input": texts,
    });

    let data = send_embedding_request(client, config, body)?;
    let data = data
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| VectorLifecycleError::EmbeddingResponse("missing data array".to_string()))?;
    if data.len() != texts.len() {
        return Err(VectorLifecycleError::EmbeddingResponse(format!(
            "embedding response returned {} vector(s) for {} input(s)",
            data.len(),
            texts.len()
        )));
    }

    let mut ordered = vec![None; texts.len()];
    for (position, item) in data.iter().enumerate() {
        let index = item
            .get("index")
            .and_then(Value::as_u64)
            .and_then(|index| usize::try_from(index).ok())
            .unwrap_or(position);
        if index >= texts.len() || ordered[index].is_some() {
            return Err(VectorLifecycleError::EmbeddingResponse(
                "embedding response contained an invalid index".to_string(),
            ));
        }
        ordered[index] = Some(parse_embedding(item)?);
    }

    ordered
        .into_iter()
        .map(|embedding| {
            embedding.ok_or_else(|| {
                VectorLifecycleError::EmbeddingResponse(
                    "embedding response omitted an input index".to_string(),
                )
            })
        })
        .collect()
}

fn send_embedding_request(
    client: &reqwest::blocking::Client,
    config: &EmbeddingConfig,
    body: Value,
) -> Result<Value, VectorLifecycleError> {
    let url = format!("{}/embeddings", config.api_base.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);

    if let Some(key) = &config.api_key {
        req = req.header("Authorization", format!("Bearer {key}"));
    }

    let resp = req
        .send()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))?;
    if !resp.status().is_success() {
        let status = resp.status().as_u16();
        let body = resp.text().unwrap_or_default();
        return Err(VectorLifecycleError::EmbeddingHttp { status, body });
    }

    resp.json()
        .map_err(|err| VectorLifecycleError::EmbeddingResponse(err.to_string()))
}

fn parse_embedding(value: &Value) -> Result<Vec<f32>, VectorLifecycleError> {
    let embedding = value
        .get("embedding")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse("missing embedding array".to_string())
        })?
        .iter()
        .map(|value| {
            let f = value.as_f64().ok_or_else(|| {
                VectorLifecycleError::EmbeddingResponse(
                    "embedding array contains a non-number".to_string(),
                )
            })?;
            let converted = f as f32;
            if !f.is_finite() || converted.is_infinite() {
                return Err(VectorLifecycleError::EmbeddingResponse(
                    "embedding contains value outside f32 range".to_string(),
                ));
            }
            Ok(converted)
        })
        .collect::<Result<Vec<_>, _>>()?;

    if embedding.is_empty() {
        return Err(VectorLifecycleError::EmbeddingResponse(
            "embedding vector was empty".to_string(),
        ));
    }
    Ok(embedding)
}

pub fn embed_query(config: &EmbeddingConfig, text: &str) -> Option<Vec<f32>> {
    let prefix = config.query_prefix.as_deref().unwrap_or("").trim();
    let input = if prefix.is_empty() {
        text.to_string()
    } else {
        format!("{prefix} {text}")
    };
    let client = match embedding_client(config) {
        Ok(client) => client,
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            return None;
        }
    };
    match embed_text(&client, config, &input) {
        Ok(embedding) => Some(embedding),
        Err(error) => {
            eprintln!("gcode: query embedding failed: {error}");
            None
        }
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
    use gobby_core::config::{ConfigSource, ai_keys, embedding_keys};
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
                "$secret:EMBEDDING_KEY" => Ok("resolved-embedding-key".to_string()),
                value => Ok(value.to_string()),
            }
        }
    }

    #[test]
    fn resolves_via_shared_routing() {
        let mut auto_source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "auto"),
            (ai_keys::EMBEDDINGS_TRANSPORT, "openai_compatible_http"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://embeddings.local:11434/v1",
            ),
        ]);
        let config = crate::config::resolve_embedding_config_from_source(None, &mut auto_source)
            .expect("auto route with endpoint should use direct embeddings");
        assert_eq!(config.api_base, "http://embeddings.local:11434/v1");

        let mut daemon_source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "daemon"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://daemon-should-not-be-used:11434/v1",
            ),
        ]);
        assert!(
            crate::config::resolve_embedding_config_from_source(None, &mut daemon_source).is_none()
        );

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
            (ai_keys::EMBEDDINGS_ROUTING, "direct"),
            (ai_keys::EMBEDDINGS_TRANSPORT, "openai_compatible_http"),
            (
                ai_keys::EMBEDDINGS_API_BASE,
                "http://shared-binding.local:11434/v1",
            ),
            (ai_keys::EMBEDDINGS_MODEL, "shared-embed-model"),
            (ai_keys::EMBEDDINGS_API_KEY, "$secret:EMBEDDING_KEY"),
            (embedding_keys::AI_QUERY_PREFIX, "query:"),
            (embedding_keys::AI_TIMEOUT_SECONDS, "12"),
        ]);

        let config = crate::config::resolve_embedding_config_from_source(None, &mut source)
            .expect("embedding config from shared binding");

        assert_eq!(config.api_base, "http://shared-binding.local:11434/v1");
        assert_eq!(config.model, "shared-embed-model");
        assert_eq!(config.api_key.as_deref(), Some("resolved-embedding-key"));
        assert_eq!(config.query_prefix.as_deref(), Some("query:"));
        assert_eq!(config.timeout_seconds, 12);
    }

    #[test]
    fn direct_source_uses_resolved_embedding_config() {
        let mut source = TestSource::with_values([
            (ai_keys::EMBEDDINGS_ROUTING, "direct"),
            (ai_keys::EMBEDDINGS_TRANSPORT, "openai_compatible_http"),
            (ai_keys::EMBEDDINGS_API_BASE, "http://resolved.local/v1"),
            (ai_keys::EMBEDDINGS_MODEL, "resolved-embed-model"),
        ]);
        let context = AiContext::resolve(None, &mut source);
        let direct_config = EmbeddingConfig {
            api_base: "http://resolved.local/v1".to_string(),
            model: "resolved-embed-model".to_string(),
            api_key: None,
            query_prefix: None,
            timeout_seconds: 10,
        };

        let source =
            embedding_source_from_resolved_ai_context(context, Some(direct_config.clone()));

        match source {
            Some(EmbeddingSource::Direct(config)) => assert_eq!(config, direct_config),
            other => panic!("expected direct embedding source, got {other:?}"),
        }
    }
}
