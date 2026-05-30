pub(crate) mod backlinks;
pub(crate) mod collect;
pub(crate) mod index;
pub(crate) mod init;
pub(crate) mod search;
pub(crate) mod setup;
pub(crate) mod status;

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
