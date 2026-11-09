//! The operator account's bootstrap credential, resolved from the passwd entry.
//!
//! Mirrors `gobby.utils.local_token.operator_bootstrap_path`: only the passwd entry selects the
//! account home. HOME and GOBBY_HOME are environment variables a caller can set, so neither can
//! redirect the credential the sandbox denies.

use std::ffi::{CStr, OsString};
use std::io;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};

/// Starting `getpwuid_r` buffer; grown on `ERANGE` up to `MAX_PASSWD_BUFFER`.
const INITIAL_PASSWD_BUFFER: usize = 1024;
const MAX_PASSWD_BUFFER: usize = 1 << 20;

/// The operator's bootstrap credential at the account's canonical location, or `None` when the
/// account has no passwd entry.
pub fn operator_bootstrap_path() -> io::Result<Option<PathBuf>> {
    // SAFETY: getuid cannot fail and takes no pointers.
    let uid = unsafe { libc::getuid() };
    Ok(account_home_for_uid(uid)?.map(|home| operator_bootstrap_for_home(&home)))
}

/// The passwd home for `uid`. `Ok(None)` means no passwd entry; any other lookup failure is an
/// error, so the caller fails closed instead of dropping the operator root.
pub(super) fn account_home_for_uid(uid: libc::uid_t) -> io::Result<Option<PathBuf>> {
    let mut buffer = vec![0_u8; INITIAL_PASSWD_BUFFER];
    loop {
        match passwd_home(uid, &mut buffer) {
            Err(libc::ERANGE) if buffer.len() < MAX_PASSWD_BUFFER => {
                buffer.resize(buffer.len() * 2, 0);
            }
            Err(code) => return Err(io::Error::from_raw_os_error(code)),
            Ok(None) => return Ok(None),
            Ok(Some(home)) => return Ok(Some(PathBuf::from(OsString::from_vec(home)))),
        }
    }
}

/// The bootstrap credential under an account home.
pub(super) fn operator_bootstrap_for_home(home: &Path) -> PathBuf {
    home.join(".gobby").join("bootstrap.yaml")
}

/// One `getpwuid_r` call. `Ok(None)` when no entry exists; `Err` carries the error code.
fn passwd_home(uid: libc::uid_t, buffer: &mut [u8]) -> Result<Option<Vec<u8>>, libc::c_int> {
    // SAFETY: `passwd` and `result` are live for the call, and `buffer` is writable for the
    // `buffer.len()` bytes passed alongside it. `pw_dir` points into `buffer`, so its bytes are
    // copied out before this function returns and before the caller reuses the buffer.
    unsafe {
        let mut passwd: libc::passwd = std::mem::zeroed();
        let mut result: *mut libc::passwd = std::ptr::null_mut();
        let status = libc::getpwuid_r(
            uid,
            &mut passwd,
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut result,
        );
        if status != 0 {
            return Err(status);
        }
        if result.is_null() {
            return Ok(None);
        }
        if passwd.pw_dir.is_null() {
            return Err(libc::EINVAL);
        }
        Ok(Some(CStr::from_ptr(passwd.pw_dir).to_bytes().to_vec()))
    }
}

#[cfg(test)]
#[path = "operator_account/tests.rs"]
mod tests;
