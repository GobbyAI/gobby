use std::path::Path;

use serde_json::json;

use crate::collect::{self as wiki_collect, CollectReport};
use crate::commands::index::{connect_postgres_index, postgres_store_for_search};
use crate::support::env::database_url_for;
use crate::support::scope::{
    resolve_command_scope, resolved_scope_identity, search_scope_for_resolved,
};
use crate::support::time::collect_timestamp;
use crate::{CommandOutcome, ScopeIdentity, ScopeSelection, WikiError, vault};

pub(crate) fn execute(selection: ScopeSelection) -> Result<CommandOutcome, WikiError> {
    let scope = resolve_command_scope(&selection)?;

    // Vault initialization is idempotent here; collect only needs the paths to exist.
    vault::initialize(&scope)?;
    let output_scope = resolved_scope_identity(&scope);
    let database_url = database_url_for("gwiki collect")?
        .ok_or_else(|| WikiError::from(gobby_core::grant::GrantError::DaemonRequired))?;
    let mut conn = connect_postgres_index(&database_url, "gwiki collect")?;
    let search_scope = search_scope_for_resolved(&scope);
    let timestamp = collect_timestamp()?;
    let report = {
        let mut store = postgres_store_for_search(&mut conn, &search_scope);
        wiki_collect::collect_inbox_and_index(scope.root(), &mut store, &timestamp)?
    };
    Ok(render(output_scope, scope.root(), report))
}

fn render(scope: ScopeIdentity, root: &Path, report: CollectReport) -> CommandOutcome {
    let accepted = report.accepted.len();
    let skipped = report.skipped.len();
    let payload = json!({
        "command": "collect",
        "scope": scope,
        "status": "ready",
        "root": root.display().to_string(),
        "accepted": accepted,
        "skipped": skipped,
        "actions": report,
    });
    let text = format!(
        "Collect ready
Scope: {scope}
Root: {}
Accepted: {accepted}
Skipped: {skipped}",
        root.display()
    );
    super::scoped_outcome("collect", &scope, payload, text)
}
