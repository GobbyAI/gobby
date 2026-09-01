//! Primary-index checkout fence.
//!
//! Primary writes are fenced on `project_checkouts.root_path` (FOR SHARE) for
//! this machine. The fence itself lives inside each write statement so the
//! checkout row stays locked until the writer's transaction ends; this module
//! runs the same check up front, before any row is written, and turns a
//! failed fence into a typed error that names the root gcode computed next to
//! the root(s) this machine has registered.

use std::path::Path;

use postgres::GenericClient;
use uuid::Uuid;

use crate::cli_error::CliError;

/// Root paths registered for `project_id` on `machine_id`, share-locked for
/// the remainder of the caller's transaction.
pub(crate) fn registered_roots(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
) -> anyhow::Result<Vec<String>> {
    let rows = conn.query(
        "SELECT root_path
         FROM project_checkouts
         WHERE machine_id = $1 AND project_id = $2
         ORDER BY root_path
         FOR SHARE",
        &[machine_id, project_id],
    )?;
    rows.iter()
        .map(|row| {
            row.try_get::<_, String>("root_path")
                .map_err(anyhow::Error::from)
        })
        .collect()
}

/// Fence a primary write on the committed checkout for `root_path` before the
/// caller writes anything, so a failed fence leaves no rows behind.
pub(crate) fn require_registered_checkout(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
    root_path: &str,
) -> anyhow::Result<()> {
    let registered = registered_roots(conn, machine_id, project_id)?;
    if registered.iter().any(|root| root == root_path) {
        return Ok(());
    }
    Err(fence_error(project_id, root_path, registered).into())
}

/// The explicit error for a fenced write statement that affected no rows. The
/// registered roots are re-read so the message reflects the committed state
/// the statement raced against.
pub(crate) fn mismatch_error(
    conn: &mut impl GenericClient,
    machine_id: &Uuid,
    project_id: &Uuid,
    root_path: &str,
) -> anyhow::Error {
    match registered_roots(conn, machine_id, project_id) {
        Ok(registered) => fence_error(project_id, root_path, registered).into(),
        Err(error) => error.context(format!(
            "primary index root {root_path} failed the checkout fence for project {project_id}, \
             and the checkouts registered on this machine could not be read"
        )),
    }
}

/// `checkout_required` when this machine registers no checkout for the
/// project at all, `checkout_mismatch` when it registers other root(s).
pub(crate) fn fence_error(project_id: &Uuid, root_path: &str, registered: Vec<String>) -> CliError {
    let project = project_id.to_string();
    let root = Path::new(root_path);
    if registered.is_empty() {
        return CliError::checkout_required(
            root,
            Some(&project),
            format!(
                "project {project} has no checkout registered on this machine; \
                 gcode computed root {root_path}"
            ),
        );
    }
    CliError::checkout_mismatch(&project, root, &registered)
}

#[cfg(test)]
#[path = "checkout_fence/tests.rs"]
mod tests;
