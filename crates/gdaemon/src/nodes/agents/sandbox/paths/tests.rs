use std::path::{Path, PathBuf};

use super::{credential_roots, operator_bootstrap_path};

/// The account's home from the passwd database, read without the production lookup.
fn passwd_home() -> PathBuf {
    use std::os::unix::ffi::OsStrExt;

    // SAFETY: an all-zero passwd is a valid placeholder that getpwuid_r overwrites on success.
    let mut entry: libc::passwd = unsafe { std::mem::zeroed() };
    let mut buffer = vec![0_u8; 16_384];
    let mut found: *mut libc::passwd = std::ptr::null_mut();
    // SAFETY: entry, buffer and found are valid for writes, and buffer.len() bounds what
    // getpwuid_r writes into it.
    let status = unsafe {
        libc::getpwuid_r(
            libc::getuid(),
            &mut entry,
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            &mut found,
        )
    };
    assert!(
        status == 0 && !found.is_null(),
        "the test account has a passwd entry"
    );
    // SAFETY: found is non-null and points at entry, whose pw_dir is a NUL-terminated string
    // stored in buffer, which outlives this read.
    let home = unsafe { std::ffi::CStr::from_ptr(entry.pw_dir) };
    PathBuf::from(std::ffi::OsStr::from_bytes(home.to_bytes()))
}

#[test]
fn credential_roots_deny_the_passwd_operator_bootstrap_under_a_gobby_home_override() {
    let gobby = Path::new("/nonexistent-override/.gobby");
    let canonical = passwd_home().join(".gobby").join("bootstrap.yaml");

    let roots = credential_roots(gobby);

    assert!(
        roots.contains(&canonical),
        "the operator's canonical bootstrap {} stays denied under a GOBBY_HOME override, got {roots:?}",
        canonical.display()
    );
}

#[test]
fn operator_bootstrap_path_is_the_passwd_home_bootstrap() {
    assert_eq!(
        operator_bootstrap_path(),
        Some(passwd_home().join(".gobby").join("bootstrap.yaml"))
    );
}
