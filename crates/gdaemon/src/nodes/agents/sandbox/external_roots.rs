//! Accept-time checks for the external write roots a hub forwards with `agent.spawn`. The node
//! is the enforcement point: each root must resolve on this node's own disk, and the run is
//! refused before it is accepted when one does not.

use super::paths;
use std::{collections::BTreeMap, fs, path::Path};

/// Refuses a root this node cannot grant. The `Err` text is the message the hub reports.
pub fn validate_external_write_roots(
    roots: &[String],
    gobby: &Path,
    home: &Path,
    env: &BTreeMap<String, String>,
) -> Result<(), String> {
    roots
        .iter()
        .try_for_each(|root| validate_root(root, gobby, home, env))
}

fn validate_root(
    root: &str,
    gobby: &Path,
    home: &Path,
    env: &BTreeMap<String, String>,
) -> Result<(), String> {
    if !Path::new(root).is_absolute() {
        return Err(format!("External write root must be absolute: {root}"));
    }
    let canonical = fs::canonicalize(root)
        .map_err(|_| format!("Target node cannot resolve external write root: {root}"))?;
    if !canonical.is_dir() {
        return Err(format!(
            "External write root must be an existing directory: {root}"
        ));
    }
    let home_canonical = fs::canonicalize(home).ok();
    if canonical.parent().is_none() || home_canonical.as_deref() == Some(canonical.as_path()) {
        return Err(format!(
            "Filesystem and home roots cannot be granted: {root}"
        ));
    }
    // The same contract the policy compiler enforces at prepare, applied before acceptance.
    let allowed = [paths::text(&canonical)];
    paths::assert_sensitive_path_contract(&[allowed.as_slice()], gobby, home, env)
        .map_err(|error| format!("External write root overlaps a protected root: {root} ({error})"))
}

#[cfg(test)]
#[path = "external_roots/tests.rs"]
mod tests;
