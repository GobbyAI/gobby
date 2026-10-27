use super::{SOCKET_DIR_OVERRIDE, current_uid, socket_directory};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};

/// Long enough that `<home>/runtime/srt-sock` overflows the 63-byte socket budget.
const LONG_HOME: &str =
    "/gobby-socket-rule-test/a-home-directory-name-long-enough-to-overflow-the-socket-budget";
const SHORT_HOME: &str = "/gobby-socket-rule-test/h";

fn no_env() -> BTreeMap<String, String> {
    BTreeMap::new()
}

/// The sixteen-hex key that `srt_socket_directory` derives from the resolved home.
fn home_key(home: &str) -> String {
    format!("{:x}", Sha256::digest(home.as_bytes()))[..16].to_string()
}

/// The shared `/tmp/gobby-srt-<uid>` root with `/tmp` resolved, as the Python rule builds it.
fn shared_root() -> anyhow::Result<PathBuf> {
    Ok(fs::canonicalize("/tmp")?.join(format!("gobby-srt-{}", current_uid())))
}

#[test]
fn long_home_socket_dir_is_keyed_by_home_digest() -> anyhow::Result<()> {
    let directory = socket_directory(Path::new(LONG_HOME), &no_env());
    assert_eq!(directory, shared_root()?.join(home_key(LONG_HOME)));
    Ok(())
}

#[test]
fn short_home_socket_dir_sits_under_home_runtime() {
    let directory = socket_directory(Path::new(SHORT_HOME), &no_env());
    assert_eq!(
        directory,
        PathBuf::from(SHORT_HOME).join("runtime").join("srt-sock")
    );
}

#[test]
fn socket_dir_override_wins_over_both_rules() {
    let env = BTreeMap::from([(
        SOCKET_DIR_OVERRIDE.to_string(),
        "/gobby-socket-rule-test/isolated".to_string(),
    )]);
    let expected = PathBuf::from("/gobby-socket-rule-test/isolated");
    assert_eq!(socket_directory(Path::new(LONG_HOME), &env), expected);
    assert_eq!(socket_directory(Path::new(SHORT_HOME), &env), expected);
}
