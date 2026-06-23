use super::super::*;

/// Curated, human-authored descriptor for one infrastructure boundary: what
/// the service is and how the workspace's code uses it, the real adapter module
/// to cite, and the graceful-degradation behavior when the service is
/// unavailable. The adapter-module `path:line` citation is the binding "cites a
/// real module" fact a test guards against renames.
pub(crate) struct InfraDescriptor {
    /// What the service is and how the code reaches it. Plain prose, no LLM.
    pub(crate) summary: &'static str,
    /// `path:line` citation for the adapter module that talks to this service:
    /// the workspace-relative module path plus a representative line pointing at
    /// the module's primary public item / public surface. The path is verified
    /// to exist on disk and the line is range-checked by the infrastructure test.
    pub(crate) adapter_module: &'static str,
    /// Documented graceful-degradation behavior when the service is
    /// unavailable, phrased from the workspace's degradation vocabulary
    /// (`gobby_core::degradation`) and the documented search/ingest fallbacks.
    pub(crate) degradation: &'static str,
}

/// Map a [`ServiceKind`] to its curated infrastructure descriptor. Every kind
/// the system model can surface has a descriptor; a kind with no descriptor is
/// defensively skipped by the builder. The adapter-module `path:line` citations
/// point at the real feature-gated / always-compiled adapter modules in the
/// workspace.
pub(crate) fn infra_descriptor(kind: ServiceKind) -> Option<InfraDescriptor> {
    let descriptor = match kind {
        ServiceKind::Postgres => InfraDescriptor {
            summary: "The Gobby PostgreSQL hub stores indexed symbols, content chunks, and \
                config. The CLIs connect read-only or read-write through the `postgres` \
                adapter feature and run pg_search BM25 queries against the hub.",
            adapter_module: "crates/gcore/src/postgres.rs:16",
            degradation: "BM25 search works whenever the PostgreSQL hub is configured and \
                indexed; with no hub configured the index-backed commands have nothing to \
                read.",
        },
        ServiceKind::Falkor => InfraDescriptor {
            summary: "FalkorDB holds the code/relationship graph projection. The `falkor` \
                adapter feature opens a read-only graph client used for graph queries and \
                the search graph-relevance boost.",
            adapter_module: "crates/gcore/src/falkor.rs:28",
            degradation: "When FalkorDB is down, graph commands return `[]` and the graph \
                boost is simply omitted from search ranking; BM25 and semantic results are \
                unaffected.",
        },
        ServiceKind::Qdrant => InfraDescriptor {
            summary: "Qdrant stores per-project vector collections (e.g. \
                `code_symbols_{project_id}`). The `qdrant` adapter feature upserts and \
                searches those vectors to power semantic retrieval.",
            adapter_module: "crates/gcore/src/qdrant.rs:20",
            degradation: "When Qdrant (or the embedding API) is unavailable, semantic \
                search returns `[]` and results fall back to BM25 plus any available graph \
                boost.",
        },
        ServiceKind::EmbeddingApi => InfraDescriptor {
            summary: "An OpenAI-compatible embedding API turns text into vectors for \
                semantic search and ingest. The `ai` adapter feature calls it directly \
                (standalone routing) using the resolved runtime config.",
            adapter_module: "crates/gcore/src/ai/embeddings.rs:19",
            degradation: "When the embedding API is routed off or unreachable, semantic \
                search returns `[]` and embedding-dependent ingest degrades to derived \
                output with explicit degradation markers.",
        },
        ServiceKind::Daemon => InfraDescriptor {
            summary: "The Gobby daemon is the optional routing target for AI work \
                (generation, embedding, transcription, vision). The `ai` adapter routes \
                requests through it when daemon routing is selected; the daemon URL is \
                resolved by always-compiled `gobby_core::daemon_url`.",
            adapter_module: "crates/gcore/src/ai/daemon.rs:10",
            degradation: "When the daemon is unreachable, AI capabilities fall back to \
                direct transport or are reported off, and ingest/generation degrade to \
                skeleton/derived output with explicit degradation markers.",
        },
        ServiceKind::GhookInbox => InfraDescriptor {
            summary: "`ghook` always enqueues each hook envelope to `~/.gobby/hooks/inbox/` \
                before attempting a best-effort daemon POST, so hooks are durable even when \
                the daemon is down (enqueue-first transport).",
            adapter_module: "crates/ghook/src/transport.rs:31",
            degradation: "When the daemon POST fails, the enqueued envelope still lands in \
                the inbox for later delivery; the observable per-CLI hook contract \
                (stdout/stderr/exit code) is preserved.",
        },
        ServiceKind::TreeSitter => InfraDescriptor {
            summary: "tree-sitter grammars drive AST-aware symbol extraction during \
                indexing. The `gcode` indexer maps file extensions to grammars and walks \
                each parse tree to extract symbols.",
            adapter_module: "crates/gcode/src/index/languages.rs:9",
            degradation: "A file in a language with no registered grammar is indexed as \
                content-only repo text (BM25/semantic) rather than producing AST symbols; \
                indexing never fails on an unknown language.",
        },
        ServiceKind::DocumentToolchain => InfraDescriptor {
            summary: "The document toolchain (PDF text/page extraction plus spreadsheet \
                parsing) lets `gwiki` ingest documents into the Markdown vault. It is \
                gated behind the `documents` Cargo feature.",
            adapter_module: "crates/gwiki/src/ingest/pdf/ingest.rs:23",
            degradation: "When the `documents` feature is off or extraction fails, the \
                ingest path falls back to a skeleton/derived Markdown document carrying an \
                explicit degradation marker.",
        },
        ServiceKind::MediaToolchain => InfraDescriptor {
            summary: "The media toolchain shells out to ffmpeg (a system binary on `PATH`) \
                to probe duration and extract audio/frames so `gwiki` can transcribe and \
                analyze audio/video sources.",
            adapter_module: "crates/gwiki/src/media.rs:13",
            degradation: "When ffmpeg is not on `PATH`, media ingest degrades (detected via \
                `error.rs::is_ffmpeg_unavailable`) to derived output with explicit \
                degradation markers instead of failing the run.",
        },
    };
    Some(descriptor)
}

/// Build the deterministic infra-stack page (#892) from the workspace
/// [`SystemModel`]. Mirrors the architecture diagrams: returns `None` when no
/// model is supplied (the AI-off / test entry points), which omits the page
/// entirely. Otherwise emits one [`InfraSection`] per service boundary, looked
/// up in the curated descriptor map; a boundary kind with no descriptor is
/// defensively skipped. Never degrades — `degraded_sources` is always empty.
pub(crate) fn build_infrastructure_doc(
    system_model: Option<&SystemModel>,
) -> Option<InfrastructureDoc> {
    let model = system_model?;

    let mut sections: Vec<InfraSection> = model
        .services
        .iter()
        .filter_map(|boundary| {
            let descriptor = infra_descriptor(boundary.kind)?;
            Some(InfraSection {
                service: boundary.name.clone(),
                pulled_in_by: boundary.pulled_in_by.clone(),
                adapter_module: descriptor.adapter_module.to_string(),
                summary: descriptor.summary.to_string(),
                degradation: descriptor.degradation.to_string(),
            })
        })
        .collect();
    sections.sort_by(|a, b| a.service.cmp(&b.service));

    Some(InfrastructureDoc {
        sections,
        // Deterministic, non-degrading: derived solely from Cargo manifests +
        // service boundaries.
        degraded_sources: Vec::new(),
    })
}
