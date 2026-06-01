use crate::support::scope::research_scope_identity;
use crate::{CommandOutcome, ScopeIdentity, WikiError, research};

#[derive(serde::Serialize)]
struct ResearchOutput {
    command: &'static str,
    scope: ScopeIdentity,
    status: &'static str,
    session: crate::session::ResearchSession,
}

pub(crate) fn execute(options: research::ResearchOptions) -> Result<CommandOutcome, WikiError> {
    let outcome = research::run(options)?;
    let session = outcome.session;
    let message = outcome.message;
    let output_scope = research_scope_identity(&session.scope);
    let payload = serde_json::to_value(ResearchOutput {
        command: "research",
        scope: output_scope.clone(),
        status: "checkpointed",
        session,
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
