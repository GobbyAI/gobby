use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use super::*;

#[test]
fn gobby_home_defaults_under_the_user_home_when_unset_or_blank() {
    let user = Path::new("/home/gobby-user");
    let default = Some(PathBuf::from("/home/gobby-user/.gobby"));
    assert_eq!(gobby_home(None, Some(user)), default);
    assert_eq!(gobby_home(Some(OsStr::new("")), Some(user)), default);
    assert_eq!(gobby_home(Some(OsStr::new("   ")), Some(user)), default);
}

#[test]
fn gobby_home_expands_a_leading_tilde_and_keeps_absolute_paths() {
    let user = Path::new("/home/u");
    assert_eq!(
        gobby_home(Some(OsStr::new("~/gh")), Some(user)),
        Some(PathBuf::from("/home/u/gh"))
    );
    assert_eq!(
        gobby_home(Some(OsStr::new("~")), Some(user)),
        Some(PathBuf::from("/home/u"))
    );
    assert_eq!(
        gobby_home(Some(OsStr::new("/srv/gobby")), Some(user)),
        Some(PathBuf::from("/srv/gobby"))
    );
}

#[test]
fn gobby_home_is_unresolvable_without_an_override_or_a_user_home() {
    assert_eq!(gobby_home(None, None), None);
}

#[test]
fn an_explicit_logs_dir_beats_the_logging_env_which_beats_the_home_logs() {
    let home = Path::new("/home/u/.gobby");
    let user = Some(Path::new("/home/u"));
    assert_eq!(
        logs_dir(
            Some(Path::new("/var/x")),
            Some(OsStr::new("/env")),
            home,
            user
        ),
        PathBuf::from("/var/x")
    );
    assert_eq!(
        logs_dir(None, Some(OsStr::new("/env")), home, user),
        PathBuf::from("/env")
    );
    assert_eq!(
        logs_dir(None, Some(OsStr::new("")), home, user),
        PathBuf::from("/home/u/.gobby/logs")
    );
    assert_eq!(
        logs_dir(None, None, home, user),
        PathBuf::from("/home/u/.gobby/logs")
    );
}

#[test]
fn logs_dir_expands_a_leading_tilde() {
    let home = Path::new("/home/u/.gobby");
    let user = Some(Path::new("/home/u"));
    assert_eq!(
        logs_dir(Some(Path::new("~/gobby-logs")), None, home, user),
        PathBuf::from("/home/u/gobby-logs")
    );
    assert_eq!(
        logs_dir(None, Some(OsStr::new("~/env-logs")), home, user),
        PathBuf::from("/home/u/env-logs")
    );
}
