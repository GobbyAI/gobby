//! The operator account's bootstrap credential, resolved from the account record.
//!
//! Mirrors `gobby.utils.local_token.operator_bootstrap_path`: only the account record selects the
//! home. HOME and GOBBY_HOME are environment variables a caller can set, so neither can redirect
//! the credential the sandbox denies. POSIX reads the passwd entry; native Windows reads the
//! profile folder from the known-folder API, which `dirs::home_dir` resolves there.

use std::io;
use std::path::{Path, PathBuf};

/// The operator's bootstrap credential at the account's canonical location, or `None` when the
/// account has no passwd entry. The passwd entry is the only source: there is no HOME fallback.
#[cfg(unix)]
pub fn operator_bootstrap_path() -> io::Result<Option<PathBuf>> {
    Ok(account_home_for_uid(nix::unistd::Uid::current())?
        .map(|home| operator_bootstrap_for_home(&home)))
}

/// The passwd home for `uid`. `Ok(None)` means no passwd entry; any other lookup failure is an
/// error, so the caller fails closed instead of dropping the operator root.
#[cfg(unix)]
pub(super) fn account_home_for_uid(uid: nix::unistd::Uid) -> io::Result<Option<PathBuf>> {
    let user = nix::unistd::User::from_uid(uid).map_err(io::Error::from)?;
    Ok(user.map(|user| user.dir))
}

/// The profile folder of the calling account, from the known-folder API. Never USERPROFILE.
#[cfg(not(unix))]
pub fn operator_bootstrap_path() -> io::Result<Option<PathBuf>> {
    Ok(dirs::home_dir().map(|home| operator_bootstrap_for_home(&home)))
}

/// The bootstrap credential under an account home.
pub(super) fn operator_bootstrap_for_home(home: &Path) -> PathBuf {
    home.join(".gobby").join("bootstrap.yaml")
}

#[cfg(test)]
#[path = "operator_account/tests.rs"]
mod tests;
