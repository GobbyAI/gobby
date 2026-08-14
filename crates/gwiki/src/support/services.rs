//! Shared runtime-service probing for commands that degrade gracefully.
//!
//! `status`, `trust`, and `librarian` all need the same answer: which
//! optional services (FalkorDB shared code graph, Qdrant + embeddings,
//! text-generation model provider) are actually reachable from the hub
//! config. This module owns that probing so each command builds its
//! availability picture from one resolver instead of re-implementing the
//! config plumbing.

use gobby_core::ai::effective_config::ai_source_for_conn;
use gobby_core::ai::generation::GenerationTier;
use gobby_core::ai::resolve_route_observed;
use gobby_core::ai_context::{AiContext, AiContextOptions};
use gobby_core::config::{
    AiCapability, AiRouting, EmbeddingConfig, FalkorConfig, QdrantConfig, resolve_embedding_config,
    resolve_falkordb_config, resolve_qdrant_config,
};

use crate::WikiError;
use crate::search::semantic::{
    GobbyQdrantBackend, GobbySemanticBackend, OpenAiEmbeddingBackend, SemanticEmbedding,
};

/// Snapshot of the optional runtime services resolvable from the hub config.
#[derive(Debug, Clone)]
pub(crate) struct RuntimeServices {
    pub(crate) postgres_configured: bool,
    pub(crate) falkor: Option<FalkorConfig>,
    pub(crate) qdrant: Option<QdrantConfig>,
    pub(crate) embedding: Option<EmbeddingConfig>,
    /// Query-embedding route resolved alongside the raw configs; carries the
    /// AI context needed for daemon-routed embeddings.
    pub(crate) semantic_embedding: Option<SemanticEmbedding>,
}

impl RuntimeServices {
    /// No hub database configured: every optional service is unavailable.
    pub(crate) fn detached() -> Self {
        Self {
            postgres_configured: false,
            falkor: None,
            qdrant: None,
            embedding: None,
            semantic_embedding: None,
        }
    }

    pub(crate) fn semantic_available(&self) -> bool {
        self.qdrant.is_some() && self.semantic_embedding.is_some()
    }

    /// Live semantic search backend, when Qdrant and an embedding route are
    /// both configured.
    pub(crate) fn semantic_backend(
        &self,
    ) -> Option<GobbySemanticBackend<OpenAiEmbeddingBackend, GobbyQdrantBackend>> {
        let embedding = self.semantic_embedding.clone()?;
        let qdrant = self.qdrant.clone()?;
        Some(GobbySemanticBackend::new(
            Some(embedding),
            Some(qdrant),
            OpenAiEmbeddingBackend::new(),
            GobbyQdrantBackend,
        ))
    }
}

/// Probe the hub for the datastore-backed optional services.
///
/// Absent hub configuration resolves to [`RuntimeServices::detached`];
/// a configured-but-unreachable hub is an error, matching `status`.
pub(crate) fn probe_runtime_services(command: &'static str) -> Result<RuntimeServices, WikiError> {
    let Some(database_url) = crate::support::env::database_url_for(command)? else {
        return Ok(RuntimeServices::detached());
    };
    let mut conn = gobby_core::postgres::connect_readonly(&database_url).map_err(|error| {
        WikiError::Config {
            detail: format!("failed to connect to PostgreSQL for {command}: {error}"),
        }
    })?;
    let mut source = ai_source_for_conn(&mut conn).map_err(|error| WikiError::Config {
        detail: format!("failed to resolve runtime config for {command}: {error}"),
    })?;
    let falkor = resolve_falkordb_config(&mut source);
    let qdrant =
        resolve_qdrant_config(&mut source).filter(crate::support::config::qdrant_config_has_url);
    let embedding = resolve_embedding_config(&mut source);
    let semantic_embedding = {
        let context = AiContext::resolve(None, &mut source);
        resolve_semantic_embedding(&context, &mut source)
    };
    Ok(RuntimeServices {
        postgres_configured: true,
        falkor,
        qdrant,
        embedding,
        semantic_embedding,
    })
}

/// Whether a text-generation model provider is reachable for `requested`
/// routing at `tier`. Mirrors the route observation the compile and ask
/// transports perform before generating.
pub(crate) fn text_generation_available(
    command: &'static str,
    requested: AiRouting,
    _tier: GenerationTier,
) -> bool {
    if matches!(requested, AiRouting::Off) {
        return false;
    }
    let Ok(mut source) = crate::support::config::hub_ai_config_source(command) else {
        return false;
    };
    let context = AiContext::resolve_with_options(
        None,
        &mut source,
        AiContextOptions {
            no_ai: false,
            forced_routing: Some(requested),
        },
    );
    let observed = resolve_route_observed(&context, AiCapability::TextGenerate);
    match observed.route {
        AiRouting::Daemon => true,
        AiRouting::Off => false,
    }
}

pub(crate) fn resolve_semantic_embedding(
    context: &AiContext,
    _source: &mut impl gobby_core::config::ConfigSource,
) -> Option<SemanticEmbedding> {
    match effective_embedding_route(context) {
        AiRouting::Off => None,
        AiRouting::Daemon => {
            #[cfg(feature = "ai")]
            {
                Some(SemanticEmbedding::Daemon(Box::new(context.clone())))
            }
            #[cfg(not(feature = "ai"))]
            {
                None
            }
        }
    }
}

fn effective_embedding_route(context: &AiContext) -> AiRouting {
    #[cfg(feature = "ai")]
    {
        gobby_core::ai::effective_route(context, AiCapability::Embed)
    }
    #[cfg(not(feature = "ai"))]
    {
        match context.binding(AiCapability::Embed).routing {
            AiRouting::Off => AiRouting::Off,
            AiRouting::Daemon => {
                eprintln!(
                    "warning: gwiki was built without ai support; daemon-backed embeddings are disabled"
                );
                AiRouting::Off
            }
        }
    }
}
