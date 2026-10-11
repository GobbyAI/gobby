use std::path::Path;

use anyhow::Context as _;
use gobby_core::grant::GrantPrincipal;
use postgres::Client;

use crate::cli_error::CliError;
use crate::db;

/// Reject a foreign overlay before parent validation or read-time freshness.
/// Frozen parent/overlay authority plus the seat's current read-only review assignment.
pub(super) fn validate(
    conn: &mut Client,
    principal: &GrantPrincipal,
    requested_id: &str,
    requested_root: &Path,
) -> anyhow::Result<()> {
    if !principal.kind.is_managed() {
        return Ok(());
    }
    // Managed grants intentionally omit overlay identity. Read the frozen DB
    // binding through the same functions used by the row-level security policy.
    let scope = conn.query_one(
        "SELECT gobby_agent_auth.current_project_id()::text,
                COALESCE(gobby_agent_auth.current_code_overlay_project_id(),
                         gobby_agent_auth.current_project_id())::text,
                gobby_agent_auth.current_review_code_project_id()::text",
        &[],
    )?;
    let parent_id = scope
        .get::<_, Option<String>>(0)
        .context("managed PostgreSQL binding has no parent project")?;
    let admitted_id = scope
        .get::<_, Option<String>>(1)
        .context("managed PostgreSQL binding has no code-index scope")?;
    let review_id = scope.get::<_, Option<String>>(2);
    if requested_id == parent_id
        || requested_id == admitted_id
        || review_id.as_deref() == Some(requested_id)
    {
        return Ok(());
    }
    // Diagnostic root metadata only: never read symbols/content from the
    // requested overlay. An unindexed admitted workspace is named by its UUID.
    let admitted_root = conn
        .query_opt(
            "SELECT root_path FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &db::id_param(&principal.machine_id)?,
                &db::id_param(&admitted_id)?,
            ],
        )?
        .map(|row| row.get::<_, String>(0))
        .unwrap_or_else(|| format!("overlay {admitted_id} (not yet indexed)"));
    let requested = if requested_root.as_os_str().is_empty() {
        format!("project {requested_id}")
    } else {
        format!("worktree {} ({requested_id})", requested_root.display())
    };
    Err(CliError {
        code: "code_overlay_mismatch",
        message: format!(
            "managed gcode principal admits {admitted_root}; requested {requested} \
             is outside its frozen overlay scope"
        ),
        recovery: Some(format!(
            "run gcode in the admitted workspace {admitted_root}, or under a grant \
             issued for the requested workspace {requested}"
        )),
        exit_status: 2,
    }
    .into())
}

pub(super) fn can_write(conn: &mut Client, requested_id: &str) -> anyhow::Result<bool> {
    let row = conn.query_one(
        "SELECT COALESCE(gobby_agent_auth.current_code_overlay_project_id(),
                         gobby_agent_auth.current_project_id())::text",
        &[],
    )?;
    Ok(row.get::<_, Option<String>>(0).as_deref() == Some(requested_id))
}
