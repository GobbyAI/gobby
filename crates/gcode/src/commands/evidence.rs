//! Thin CLI adapter for exact, model-independent evidence reads.

use std::sync::{Arc, OnceLock};

use crate::cli_error::CliError;
use crate::codewiki_facts::CodewikiFacts;
use crate::config::{CODE_SYMBOL_COLLECTION_PREFIX, Context, ServiceConfigSelection};
use crate::evidence::{
    EvidenceError, EvidenceLibrary, EvidenceOperation, EvidenceRequest, FactPage, HybridIdentity,
    HybridSearch, SearchLane, SearchSelector, validate_request_shape,
};
use crate::output::{self, Format};
use crate::vector::code_symbols::{audited_semantic_search, collection_name};

fn request_recovery(action: &str) -> String {
    format!(
        r#"{action} with --request-json, for example {{"schema_version":1,"operation":"search","search":{{"lane":"symbol","query":"NAME"}}}}; run gcode evidence --help for more examples"#
    )
}

pub(crate) fn preflight(
    request_json: &str,
    format: Format,
    allow_stale: bool,
    project_override: Option<&str>,
) -> anyhow::Result<EvidenceRequest> {
    let mut document: serde_json::Value =
        serde_json::from_str(request_json).map_err(|error| CliError {
            code: "invalid_evidence_request",
            message: format!("invalid evidence request JSON: {error}"),
            recovery: Some(request_recovery("provide one evidence schema v1 request")),
            exit_status: 2,
        })?;

    if document.get("binding").is_none() {
        let root = match project_override {
            Some(value) => {
                let path = std::path::PathBuf::from(value);
                if path.is_dir() {
                    path.canonicalize()?
                } else {
                    crate::daemon::lookup_project_by_name(value)?.root
                }
            }
            None => crate::config::detect_project_root()?,
        };
        let identity = crate::config::resolve_project_identity(&root)?;
        let object = document
            .as_object_mut()
            .ok_or_else(|| anyhow::anyhow!("evidence request must be a JSON object"))?;
        object.insert(
            "binding".to_string(),
            serde_json::json!({
                "project_id": identity.project_id,
                "commit_oid": crate::git::git_output(&identity.root, &["rev-parse", "HEAD"] )?,
                "tree_oid": crate::git::git_output(&identity.root, &["rev-parse", "HEAD^{tree}"] )?,
            }),
        );
    }
    let request = serde_json::from_value(document).map_err(|error| CliError {
        code: "invalid_evidence_request",
        message: format!("invalid evidence request JSON: {error}"),
        recovery: Some(request_recovery(
            "provide a valid evidence schema v1 request",
        )),
        exit_status: 2,
    })?;

    if !matches!(format, Format::Json) {
        return Err(CliError {
            code: "unsupported_evidence_format",
            message: "evidence output is available only as JSON".to_string(),
            recovery: Some("remove --format text or pass --format json".to_string()),
            exit_status: 2,
        }
        .into());
    }
    if allow_stale {
        return Err(CliError {
            code: "stale_admission_bypass_forbidden",
            message: "--allow-stale cannot be used with deterministic evidence reads".to_string(),
            recovery: Some(
                "remove --allow-stale and repair or rebuild stale index facts before retrying"
                    .to_string(),
            ),
            exit_status: 2,
        }
        .into());
    }

    validate_request_shape(&request).map_err(cli_error)?;

    Ok(request)
}

pub(crate) fn service_config_selection(request: &EvidenceRequest) -> ServiceConfigSelection {
    match request.operation {
        EvidenceOperation::Graph { .. } => ServiceConfigSelection::falkordb_only(),
        EvidenceOperation::Search { ref search } if search.lane == SearchLane::Hybrid => {
            ServiceConfigSelection::vectors()
        }
        EvidenceOperation::Search { .. } | EvidenceOperation::Read { .. } => {
            ServiceConfigSelection::database_only()
        }
    }
}

pub(crate) fn classify_context_error(error: anyhow::Error) -> anyhow::Error {
    if error.downcast_ref::<CliError>().is_some()
        || error
            .downcast_ref::<gobby_core::grant::GrantError>()
            .is_some()
    {
        return error;
    }
    CliError {
        code: "index_unavailable",
        message: format!("index unavailable: {error:#}"),
        recovery: Some(
            "restore the managed PostgreSQL code-index connection and retry".to_string(),
        ),
        exit_status: 2,
    }
    .into()
}

pub(crate) fn run(
    ctx: &Context,
    request: EvidenceRequest,
    output_debug_files: bool,
) -> anyhow::Result<()> {
    crate::freshness::ensure_fresh(ctx, crate::freshness::FreshnessScope::Project)?;
    let facts = Arc::new(CodewikiFacts::from_context(ctx.clone()));
    let mut library = EvidenceLibrary::new(&ctx.project_root, request.binding.clone(), facts)
        .map_err(cli_error)?;
    if matches!(
        request.operation,
        EvidenceOperation::Search {
            search: SearchSelector {
                lane: SearchLane::Hybrid,
                ..
            }
        }
    ) {
        library = library.with_hybrid(Arc::new(NativeHybridSearch::new(ctx.clone())));
    }
    let mut response = library.query(request).map_err(cli_error)?;
    response.observation = Some(crate::evidence::EvidenceObservation {
        observed_at: chrono::Utc::now().to_rfc3339(),
        checkout: ctx.project_root.clone(),
        recorded_head: response.binding.commit_oid.clone(),
    });
    if output_debug_files {
        crate::debug_output::write_debug("evidence", &serde_json::to_value(&response)?);
    }
    output::print_json(&response)
}

struct NativeHybridSearch {
    ctx: Context,
    identity: OnceLock<std::result::Result<HybridIdentity, String>>,
}

impl NativeHybridSearch {
    fn new(ctx: Context) -> Self {
        Self {
            ctx,
            identity: OnceLock::new(),
        }
    }

    fn resolve_identity(&self) -> std::result::Result<HybridIdentity, String> {
        let embedding = self
            .ctx
            .embedding
            .as_ref()
            .ok_or_else(|| "effective embedding configuration is missing".to_string())?;
        let dimension = self
            .ctx
            .code_vectors
            .vector_dim
            .ok_or_else(|| "effective embedding dimension is missing".to_string())?;
        let qdrant = self
            .ctx
            .qdrant
            .as_ref()
            .ok_or_else(|| "effective vector service configuration is missing".to_string())?;
        let index_id = collection_name(CODE_SYMBOL_COLLECTION_PREFIX, &self.ctx.project_id)
            .map_err(|error| error.to_string())?;
        let schema = gobby_core::qdrant::collection_schema(qdrant, &index_id)
            .map_err(|error| format!("failed to verify semantic index {index_id:?}: {error}"))?
            .ok_or_else(|| format!("semantic index {index_id:?} does not exist"))?;
        if schema.size != Some(dimension) || schema.distance.as_deref() != Some("Cosine") {
            return Err(format!(
                "semantic index {index_id:?} identity changed: expected dimension {dimension} distance Cosine, found dimension {} distance {}",
                schema
                    .size
                    .map(|size| size.to_string())
                    .unwrap_or_else(|| "unknown".to_string()),
                schema.distance.as_deref().unwrap_or("unknown")
            ));
        }
        Ok(HybridIdentity {
            endpoint: embedding.api_base.clone(),
            model: embedding.model.clone(),
            dimension,
            index_id,
        })
    }
}

impl HybridSearch for NativeHybridSearch {
    fn effective_identity(&self) -> std::result::Result<HybridIdentity, String> {
        self.identity
            .get_or_init(|| self.resolve_identity())
            .clone()
    }

    fn search_symbol_ids(
        &self,
        selector: &SearchSelector,
    ) -> std::result::Result<FactPage<String>, String> {
        let identity = self.effective_identity()?;
        let limit = selector.limit.saturating_add(1);
        let (mut hits, backend_truncated) = audited_semantic_search(
            &self.ctx,
            &selector.query,
            limit,
            &identity.endpoint,
            &identity.model,
            identity.dimension,
        )
        .map_err(|error| error.to_string())?;
        let truncated = backend_truncated || hits.len() > selector.limit;
        hits.truncate(selector.limit);
        Ok(FactPage {
            items: hits.into_iter().map(|(symbol_id, _)| symbol_id).collect(),
            truncated,
        })
    }
}

fn cli_error(error: EvidenceError) -> CliError {
    let recovery = match &error {
        EvidenceError::BindingMismatch { .. } => "use the persisted caller repository binding",
        EvidenceError::ContinuationMismatch => {
            "reuse a continuation only with the canonical request that produced it"
        }
        EvidenceError::InvalidSelector { .. }
        | EvidenceError::StaleRange { .. }
        | EvidenceError::UnsafePath { .. } => "correct or narrow the request selector and retry",
        EvidenceError::FactMismatch { .. } => "refresh the caller index before retrying",
        EvidenceError::Git { .. } => {
            "fetch or select the exact Git commit object, then rebuild the request binding"
        }
        EvidenceError::GraphUnavailable { .. } => {
            "restore the managed graph projection or use a deterministic search/read selector"
        }
        EvidenceError::IndexUnavailable { .. } => {
            "restore the managed PostgreSQL code-index connection and retry"
        }
        EvidenceError::NarrowingRequired { .. } => {
            "narrow the selector or increase max_bytes enough for one complete evidence item"
        }
        EvidenceError::SemanticFailure { .. }
        | EvidenceError::SemanticIdentityMismatch { .. }
        | EvidenceError::SemanticIdentityRequired => {
            "use a deterministic lane or configure the exact audited semantic provider identity"
        }
        EvidenceError::Contract { .. } | EvidenceError::UnsupportedSchema { .. } => {
            "correct the evidence schema v1 request contract and retry"
        }
    };
    CliError {
        code: error.code(),
        message: error.to_string(),
        recovery: Some(recovery.to_string()),
        exit_status: 2,
    }
}
