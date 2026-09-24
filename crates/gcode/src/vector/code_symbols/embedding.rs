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
                embed_via_daemon_or_err(context, &[text.to_string()], true).and_then(|embeddings| {
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
            EmbeddingSource::Daemon(context) => {
                embed_via_daemon_or_err(context, texts, INDEXING_EMBED_QUERY_MODE)
            }
        }
    }
}

const INDEXING_EMBED_QUERY_MODE: bool = false;

fn embed_via_daemon_or_err(
    context: &AiContext,
    texts: &[String],
    is_query: bool,
) -> Result<Vec<Vec<f32>>, VectorLifecycleError> {
    #[cfg(feature = "ai")]
    {
        daemon::embed_via_daemon(context, texts, is_query)
            .map(|result| result.embeddings)
            .map_err(|error| VectorLifecycleError::EmbeddingResponse(error.to_string()))
    }
    #[cfg(not(feature = "ai"))]
    {
        let _ = (context, texts, is_query);
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

pub(super) fn audited_query_embedding(
    ctx: &Context,
    query: &str,
    expected_endpoint: &str,
    expected_model: &str,
    expected_dimension: usize,
) -> Result<Vec<f32>, VectorLifecycleError> {
    let configured = ctx
        .embedding
        .as_ref()
        .ok_or(VectorLifecycleError::MissingEmbeddingConfig)?;
    if configured.api_base != expected_endpoint || configured.model != expected_model {
        return Err(VectorLifecycleError::EmbeddingResponse(format!(
            "configured embedding identity changed: expected endpoint {expected_endpoint:?} model {expected_model:?}, found endpoint {:?} model {:?}",
            configured.api_base, configured.model
        )));
    }
    #[cfg(feature = "ai")]
    let embedding = {
        let mut source = gobby_core::ai_context::LocalAiConfigSource::empty();
        let mut context = AiContext::resolve(Some(ctx.project_id.clone()), &mut source);
        context.bindings.embed.api_base = Some(expected_endpoint.to_string());
        context.bindings.embed.model = Some(expected_model.to_string());
        attach_grant(&mut context, ctx);
        let result = daemon::embed_via_daemon(&context, &[query.to_string()], true)
            .map_err(|error| VectorLifecycleError::EmbeddingResponse(error.to_string()))?;
        if result.model != expected_model || result.dim != expected_dimension {
            return Err(VectorLifecycleError::EmbeddingResponse(format!(
                "daemon embedding identity changed: expected model {expected_model:?} dimension {expected_dimension}, found model {:?} dimension {}",
                result.model, result.dim
            )));
        }
        result.embeddings.into_iter().next().ok_or_else(|| {
            VectorLifecycleError::EmbeddingResponse(
                "daemon embedding response was empty".to_string(),
            )
        })?
    };
    #[cfg(not(feature = "ai"))]
    let embedding = {
        let _ = (ctx, query, expected_endpoint, expected_model);
        return Err(VectorLifecycleError::EmbeddingResponse(
            "gcode built without the ai feature".to_string(),
        ));
    };
    if embedding.len() != expected_dimension {
        return Err(VectorLifecycleError::EmbeddingResponse(format!(
            "query embedding returned {} dimension(s), expected {expected_dimension}",
            embedding.len()
        )));
    }
    Ok(embedding)
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
                "http://daemon-served.local:11434/v1",
            ),
        ]);
        let daemon = crate::config::resolve_embedding_config_from_source(None, &mut daemon_source)
            .expect("daemon-served embedding config");
        assert_eq!(daemon.api_base, "http://daemon-served.local:11434/v1");

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
    fn daemon_route_embed_query_and_indexing_batch_record_is_query() {
        let route = DaemonRoute::start();
        let backend =
            super::EmbeddingBackend::new(EmbeddingSource::Daemon(Box::new(daemon_embed_context())))
                .expect("daemon backend");

        let query_vector = backend.embed_query("query text").expect("query embedding");
        let query = recorded_body(&route.rx);
        let indexed = backend
            .embed_text_batch(&["document text".to_string()])
            .expect("indexing embedding");
        let indexing = recorded_body(&route.rx);

        assert_eq!(query_vector, vec![0.25, 0.5]);
        assert_eq!(indexed, vec![vec![0.25, 0.5]]);
        assert_eq!(query["is_query"], serde_json::json!(true));
        assert_eq!(indexing["is_query"], serde_json::json!(false));
        assert_eq!(query["input"], serde_json::json!(["query text"]));
        assert_eq!(indexing["input"], serde_json::json!(["document text"]));
    }

    #[cfg(feature = "ai")]
    fn recorded_body(rx: &std::sync::mpsc::Receiver<String>) -> serde_json::Value {
        let raw = rx
            .recv_timeout(std::time::Duration::from_secs(5))
            .expect("daemon embeddings request");
        let body = raw
            .split_once("\r\n\r\n")
            .map(|(_, body)| body)
            .unwrap_or("");
        serde_json::from_str(body).expect("embeddings request JSON")
    }

    #[cfg(feature = "ai")]
    fn daemon_embed_context() -> AiContext {
        let binding = gobby_core::config::CapabilityBinding {
            routing: gobby_core::config::AiRouting::Daemon,
            transport: None,
            api_base: None,
            api_key: None,
            model: Some("daemon-model".to_string()),
            provider: Some("daemon-provider".to_string()),
            task: None,
            language: None,
            target_lang: None,
            profile: None,
            candidates: None,
            reasoning_effort: None,
            verify_profile: None,
            verify_model: None,
            verify_api_key: None,
        };
        let raw = std::fs::read(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("../../tests/runtime_grants/golden/direct_datastores.json"),
        )
        .expect("golden grant");
        let grant: gobby_core::grant::GrantBundle =
            serde_json::from_slice(raw.trim_ascii()).expect("parse golden grant");
        AiContext {
            bindings: gobby_core::ai_context::AiBindings {
                embed: binding.clone(),
                audio_transcribe: binding.clone(),
                audio_translate: binding.clone(),
                vision_extract: binding.clone(),
                text_generate: binding,
            },
            tuning: gobby_core::config::AiTuning {
                max_concurrency: 1,
                keep_alive: None,
            },
            limiter: gobby_core::ai_context::AiLimiter::new(1),
            tool_loop_limits: gobby_core::ai::generation::ToolLoopLimits::default(),
            project_id: Some("project-interactive".to_string()),
            grant: Some(gobby_core::ai_context::GrantAiState {
                capabilities: grant.capabilities.clone(),
                daemon_reachable: true,
                bundle: grant,
            }),
        }
    }

    #[cfg(feature = "ai")]
    struct DaemonRoute {
        _lock: std::sync::MutexGuard<'static, ()>,
        home: Option<std::ffi::OsString>,
        daemon_url: Option<std::ffi::OsString>,
        agent_token: Option<std::ffi::OsString>,
        dir: std::path::PathBuf,
        rx: std::sync::mpsc::Receiver<String>,
    }

    #[cfg(feature = "ai")]
    static DAEMON_ROUTE_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

    #[cfg(feature = "ai")]
    impl DaemonRoute {
        fn start() -> Self {
            let _lock = DAEMON_ROUTE_ENV
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let (port, rx) = spawn_embed_server();
            let dir = std::env::temp_dir().join(format!("gobby-embed-route-{port}"));
            std::fs::create_dir_all(&dir).expect("temp home");
            std::fs::write(dir.join("local_cli_token"), "embed-token\n").expect("token");
            let url = format!("http://127.0.0.1:{port}");
            let route = Self {
                _lock,
                home: std::env::var_os("GOBBY_HOME"),
                daemon_url: std::env::var_os("GOBBY_DAEMON_URL"),
                agent_token: std::env::var_os(gobby_core::local_token::AGENT_API_TOKEN_ENV),
                dir,
                rx,
            };
            // SAFETY: DAEMON_ROUTE_ENV is held until Drop restores these variables.
            unsafe {
                std::env::set_var("GOBBY_HOME", &route.dir);
                std::env::set_var("GOBBY_DAEMON_URL", &url);
                std::env::remove_var(gobby_core::local_token::AGENT_API_TOKEN_ENV);
            }
            route
        }
    }

    #[cfg(feature = "ai")]
    impl Drop for DaemonRoute {
        fn drop(&mut self) {
            restore_env("GOBBY_HOME", self.home.as_deref());
            restore_env("GOBBY_DAEMON_URL", self.daemon_url.as_deref());
            restore_env(
                gobby_core::local_token::AGENT_API_TOKEN_ENV,
                self.agent_token.as_deref(),
            );
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[cfg(feature = "ai")]
    fn restore_env(name: &str, value: Option<&std::ffi::OsStr>) {
        // SAFETY: caller holds DAEMON_ROUTE_ENV and passes the values captured
        // before this test overwrote the variables.
        unsafe {
            match value {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
    }

    #[cfg(feature = "ai")]
    fn spawn_embed_server() -> (u16, std::sync::mpsc::Receiver<String>) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind embed server");
        let port = listener.local_addr().expect("server port").port();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            const RESPONSE: &str = r#"{"embeddings":[[0.25,0.5]],"model":"embed-model","dim":2}"#;
            for _ in 0..2 {
                let Ok((mut stream, _)) = listener.accept() else {
                    break;
                };
                let _ = stream.set_read_timeout(Some(std::time::Duration::from_secs(5)));
                let Ok(request) = read_recorded_request(&mut stream) else {
                    break;
                };
                let _ = tx.send(request);
                let _ = write_embed_response(&mut stream, RESPONSE);
            }
        });
        (port, rx)
    }

    #[cfg(feature = "ai")]
    fn read_recorded_request(stream: &mut std::net::TcpStream) -> std::io::Result<String> {
        use std::io::Read;
        let mut request = Vec::new();
        let mut chunk = [0_u8; 1024];
        loop {
            let read = stream.read(&mut chunk)?;
            if read == 0 {
                break;
            }
            request.extend_from_slice(&chunk[..read]);
            if let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                let header = String::from_utf8_lossy(&request[..header_end]);
                let Some(length) = header_content_length(&header) else {
                    break;
                };
                if request.len() >= header_end + 4 + length {
                    request.truncate(header_end + 4 + length);
                    break;
                }
            }
        }
        String::from_utf8(request)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
    }

    #[cfg(feature = "ai")]
    fn header_content_length(header: &str) -> Option<usize> {
        header.lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            if name.eq_ignore_ascii_case("content-length") {
                value.trim().parse().ok()
            } else {
                None
            }
        })
    }

    #[cfg(feature = "ai")]
    fn write_embed_response(stream: &mut std::net::TcpStream, body: &str) -> std::io::Result<()> {
        use std::io::Write;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
             Connection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(response.as_bytes())?;
        stream.flush()
    }
}
