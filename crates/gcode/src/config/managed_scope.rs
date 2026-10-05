use std::path::Path;

use gobby_core::grant::GrantPrincipal;
use postgres::Client;

use crate::cli_error::CliError;
use crate::db;

/// Reject a foreign overlay before parent validation or read-time freshness.
/// A managed principal's parent remains readable; its overlay scope is frozen.
pub(super) fn validate(
    conn: &mut Client,
    principal: &GrantPrincipal,
    requested_id: &str,
    requested_root: &Path,
) -> anyhow::Result<()> {
    if !principal.kind.is_managed() || requested_id == principal.project_id {
        return Ok(());
    }
    if principal.code_overlay_project_id.as_deref() == Some(requested_id) {
        return Ok(());
    }

    let admitted_id = principal
        .code_overlay_project_id
        .as_deref()
        .unwrap_or(&principal.project_id);
    // Diagnostic root metadata only: never read symbols/content from the
    // requested overlay. An unindexed admitted workspace is named by its UUID.
    let admitted_root = conn
        .query_opt(
            "SELECT root_path FROM code_indexed_project_states
             WHERE machine_id = $1 AND project_id = $2",
            &[
                &db::id_param(&principal.machine_id)?,
                &db::id_param(admitted_id)?,
            ],
        )?
        .map(|row| row.get::<_, String>(0))
        .unwrap_or_else(|| format!("overlay {admitted_id} (not yet indexed)"));
    Err(CliError {
        code: "code_overlay_mismatch",
        message: format!(
            "managed gcode principal admits {admitted_root}; requested worktree {} \
             ({requested_id}) is outside its frozen overlay scope",
            requested_root.display()
        ),
        recovery: Some(format!(
            "run gcode in the admitted workspace {admitted_root}, or have the lane manager \
             seat the reviewer in {} with a grant issued for that workspace",
            requested_root.display()
        )),
        exit_status: 2,
    }
    .into())
}
