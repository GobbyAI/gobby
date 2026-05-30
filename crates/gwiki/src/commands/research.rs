use crate::support::scope::research_scope_identity;
use crate::{CommandOutcome, WikiError, research};

pub(crate) fn execute(options: research::ResearchOptions) -> Result<CommandOutcome, WikiError> {
    let outcome = research::run(options)?;
    let session = outcome.session;
    let message = outcome.message;
    let output_scope = research_scope_identity(&session.scope);
    let payload = serde_json::json!({
        "command": "research",
        "scope": output_scope,
        "status": "checkpointed",
        "session": session,
    });

    Ok(super::scoped_outcome(
        "research",
        &output_scope,
        payload,
        message,
    ))
}
