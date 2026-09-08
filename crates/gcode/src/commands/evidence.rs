//! Thin CLI adapter for exact, model-independent evidence reads.

use std::sync::Arc;

use crate::cli_error::CliError;
use crate::codewiki_facts::CodewikiFacts;
use crate::config::{Context, ServiceConfigSelection};
use crate::evidence::{
    EvidenceError, EvidenceLibrary, EvidenceOperation, EvidenceRequest, Snapshot,
};
use crate::output::{self, Format};

pub(crate) fn preflight(
    request_json: &str,
    format: Format,
    allow_stale: bool,
) -> anyhow::Result<EvidenceRequest> {
    let request = serde_json::from_str(request_json).map_err(|error| CliError {
        code: "invalid_evidence_request",
        message: format!("invalid evidence request JSON: {error}"),
        recovery: Some(
            "provide one complete evidence schema v1 request with --request-json".to_string(),
        ),
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

    Ok(request)
}

pub(crate) fn service_config_selection(request: &EvidenceRequest) -> ServiceConfigSelection {
    match request.operation {
        EvidenceOperation::Graph { .. } => ServiceConfigSelection::falkordb_only(),
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

pub(crate) fn run(ctx: &Context, request: EvidenceRequest) -> anyhow::Result<()> {
    let snapshot = Snapshot::prepare(
        &ctx.project_root,
        &ctx.project_id,
        &request.binding.commit_oid,
    )
    .map_err(cli_error)?;
    let facts = Arc::new(CodewikiFacts::from_context(ctx.clone()));
    let response = EvidenceLibrary::new(snapshot, facts)
        .and_then(|library| library.query(request))
        .map_err(cli_error)?;
    output::print_json(&response)
}

fn cli_error(error: EvidenceError) -> CliError {
    let recovery = match &error {
        EvidenceError::BindingMismatch { .. }
        | EvidenceError::InventoryIncomplete
        | EvidenceError::InventoryMismatch { .. } => {
            "rebuild the request binding from the exact managed Git snapshot"
        }
        EvidenceError::ContinuationMismatch => {
            "reuse a continuation only with the canonical request that produced it"
        }
        EvidenceError::ExcludedPath { .. }
        | EvidenceError::InvalidSelector { .. }
        | EvidenceError::PathNotTracked { .. }
        | EvidenceError::StaleRange { .. }
        | EvidenceError::UnsafePath { .. } => "correct or narrow the request selector and retry",
        EvidenceError::FactMismatch { .. } | EvidenceError::IndexIncomplete { .. } => {
            "repair or rebuild index facts for the exact requested snapshot before retrying"
        }
        EvidenceError::Git { .. }
        | EvidenceError::InvalidObjectId { .. }
        | EvidenceError::MissingGitObject { .. } => {
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
