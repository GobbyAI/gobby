pub mod backlinks;
pub mod collect;
pub mod index;
pub mod init;
pub mod search;
pub mod setup;
pub mod status;

use crate::{CommandOutcome, CommandResult, ScopeIdentity};

pub(crate) fn scoped_outcome(
    command: &'static str,
    scope: &ScopeIdentity,
    payload: serde_json::Value,
    text: String,
) -> CommandOutcome {
    CommandOutcome {
        status_messages: vec![format!("{command} resolved scope {scope}")],
        result: CommandResult { payload, text },
    }
}
