use crate::support::scope::research_scope_identity;
use crate::{CommandOutcome, ScopeIdentity, WikiError, research};

#[derive(serde::Serialize)]
struct ResearchOutput {
    command: &'static str,
    scope: ScopeIdentity,
    query: String,
    audit: bool,
    status: research::ResearchStatus,
    stop_reason: research::ResearchStopReason,
    steps_used: usize,
    tokens_used: usize,
    max_steps: usize,
    max_tokens: usize,
    max_sources: usize,
    write_conflict: bool,
    sources_added: Vec<String>,
    accepted_notes: Vec<crate::session::AcceptedResearchNote>,
    findings: Vec<research::AuditFinding>,
    gaps: Vec<research::ResearchGap>,
    warnings: Vec<String>,
    changed_paths: Vec<std::path::PathBuf>,
    session_id: String,
}

pub(crate) fn execute(options: research::ResearchOptions) -> Result<CommandOutcome, WikiError> {
    let outcome = research::run(options)?;
    let message = outcome.message;
    let output_scope = research_scope_identity(&outcome.session.scope);
    let payload = serde_json::to_value(ResearchOutput {
        command: "research",
        scope: output_scope.clone(),
        query: outcome.session.question,
        audit: outcome.audit,
        status: outcome.status,
        stop_reason: outcome.stop_reason,
        steps_used: outcome.steps_used,
        tokens_used: outcome.tokens_used,
        max_steps: outcome.max_steps,
        max_tokens: outcome.max_tokens,
        max_sources: outcome.max_sources,
        write_conflict: outcome.write_conflict,
        sources_added: outcome.sources_added,
        accepted_notes: outcome.session.accepted_notes,
        findings: outcome.findings,
        gaps: outcome.gaps,
        warnings: outcome.warnings,
        changed_paths: outcome.changed_paths,
        session_id: outcome.session.session_id,
    })
    .map_err(|error| WikiError::Json {
        action: "serialize research output",
        path: None,
        source: error,
    })?;

    Ok(super::scoped_outcome(
        "research",
        &output_scope,
        payload,
        message,
    ))
}
