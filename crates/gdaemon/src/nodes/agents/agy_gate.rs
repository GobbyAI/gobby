//! The agy version gate: the port of `ensure_agy_support`.
use std::path::Path;

/// The oldest `agy` a node launches.
pub const AGY_REQUIRED_VERSION: &str = "1.1.18";

/// Probes the `agy` found on `path_env`. `Err` carries the refusal reason.
pub fn ensure_agy_support(_path_env: &str, _cwd: &Path) -> Result<(), String> {
    todo!("2.6 agy gate")
}
